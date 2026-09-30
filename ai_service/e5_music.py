"""YouTube 실시간 후보를 multilingual-e5-small로 정렬하는 음악 추천 서비스."""
from __future__ import annotations

import asyncio
import calendar
from dataclasses import dataclass
import html
from pathlib import Path
import re
import time

from ai_service.diagnostics import record
from ai_service.errors import MusicRecommendationFailed, ServiceUnavailable
from ai_service.music import YouTubeMusic, normalized
from ai_service.schemas import MusicRecommendation, MusicRequest, MusicSuggestion


MODEL_DIMENSION = 384
SEARCH_LIMIT = 10
MAX_CANDIDATES = 30
VERIFY_LIMIT = 10
UNWANTED_VIDEO = re.compile(
    r"\b(?:playlist|mix|cover|live|remix|karaoke|reaction|instrumental|"
    r"slowed|sped\s*up|compilation|full\s*album|vlog|tourism|"
    r"travel\s*(?:guide|vlog)|documentary|asmr|soundscape|nature\s*sounds?|news)\b|"
    r"플레이리스트|모음집|커버|라이브|노래방|리믹스|브이로그|여행정보|여행가이드|"
    r"자연의\s*소리|파도\s*소리|빗\s*소리|백색소음|뉴스|\[영상\]",
    re.IGNORECASE,
)
MUSIC_MARKER = re.compile(r"\b(?:official\s*(?:music\s*)?(?:video|audio)|m/?v)\b|뮤직비디오", re.IGNORECASE)
LEADING_MV = re.compile(r"^(?:\[\s*MV\s*\]\s*|MV\s*[l|:]\s*)", re.IGNORECASE)
OFFICIAL_SUFFIX = re.compile(
    r"\s*[\[(][^\])]*(?:official|music\s*video|audio|\bmv\b|m/v|lyrics?|"
    r"뮤직비디오|공식영상)[^\])]*[\])]",
    re.IGNORECASE,
)
THEME_LABELS = {
    "NATURE": "자연", "FOOD": "음식", "CULTURE": "문화", "RELAX": "휴식",
    "HEALING": "힐링", "ROMANCE": "로맨스", "ADVENTURE": "모험",
    "CITY": "도시", "BEACH": "바다",
}
SEARCH_THEMES = {
    "NATURE": "자연", "CULTURE": "문화", "RELAX": "휴식", "HEALING": "힐링",
    "ROMANCE": "로맨스", "ADVENTURE": "모험", "CITY": "도시", "BEACH": "바다",
}
SEARCH_THEMES_EN = {
    "NATURE": "nature", "CULTURE": "culture", "RELAX": "relaxing", "HEALING": "healing",
    "ROMANCE": "romantic", "ADVENTURE": "adventure", "CITY": "city", "BEACH": "beach",
}
SEASONS = {
    "SPRING": ("봄", "spring"),
    "SUMMER": ("여름", "summer"),
    "AUTUMN": ("가을", "autumn"),
    "WINTER": ("겨울", "winter"),
}
SEASON_MATCH_BONUS = 0.03


@dataclass(frozen=True)
class SongCandidate:
    entry: dict
    suggestion: MusicSuggestion
    video_title: str
    channel: str

    def passage(self) -> str:
        return (
            f"passage: 노래 {self.suggestion.title}. 가수 {self.suggestion.artist}. "
            f"영상 제목 {self.video_title}. 채널 {self.channel}."
        )


def clean_title(value: str) -> str:
    return OFFICIAL_SUFFIX.sub("", html.unescape(value)).strip(" -–—|\t")


def parse_candidate(entry: dict) -> SongCandidate | None:
    """명확한 곡명·가수가 있는 단일 영상만 후보로 만든다."""
    if not isinstance(entry, dict):
        return None
    video_id, raw_title = entry.get("id"), entry.get("title")
    channel = entry.get("channel") or entry.get("uploader")
    if (not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
            or not isinstance(raw_title, str) or not isinstance(channel, str)
            or entry.get("_type") == "playlist"
            or entry.get("live_status") in ("is_live", "is_upcoming")
            or UNWANTED_VIDEO.search(raw_title)
            or re.search(r"\[\s*MV\s+in\b", raw_title, re.IGNORECASE)
            or (isinstance(entry.get("duration"), (int, float)) and entry["duration"] > 900)):
        return None
    title = LEADING_MV.sub("", clean_title(raw_title)).strip()
    channel_artist = re.sub(r"(?:\s*[-–—]\s*Topic|VEVO)$", "", channel, flags=re.IGNORECASE).strip()
    is_music_channel = channel_artist != channel
    parts = re.split(r"\s+[-–—|]\s+", title, maxsplit=1)
    if len(parts) == 2:
        artist, song_title = parts
        if re.search(r"\s+[-–—|]\s+", song_title):
            return None
        # 곡명·가수의 순서가 모호한 임의의 영상 제목은 사용하지 않는다.
        if not (normalized(artist) in normalized(channel_artist)
                or is_music_channel and normalized(channel_artist) in normalized(artist)
                or MUSIC_MARKER.search(raw_title)):
            return None
    else:
        if not is_music_channel:
            return None
        artist, song_title = channel_artist, title
    artist, song_title = artist.strip(), song_title.strip()
    if not artist or not song_title or len(artist) > 120 or len(song_title) > 200:
        return None
    try:
        suggestion = MusicSuggestion(title=song_title, artist=artist)
    except ValueError:
        return None
    return SongCandidate(entry, suggestion, raw_title, channel)


def season_for_month(month: int) -> str:
    """여행 시작일의 한국 시간 월을 사계절에 매핑한다."""
    if month in (3, 4, 5):
        return "SPRING"
    if month in (6, 7, 8):
        return "SUMMER"
    if month in (9, 10, 11):
        return "AUTUMN"
    if month in (12, 1, 2):
        return "WINTER"
    raise ValueError("month must be between 1 and 12")


def mentioned_seasons(song_title: str) -> set[str]:
    """곡명에 명시된 계절만 추출한다. 중의적인 영어 fall은 판정에 쓰지 않는다."""
    return {
        season for season, (korean, english) in SEASONS.items()
        if korean in song_title or re.search(rf"\b{english}\b", song_title, re.IGNORECASE)
    }


def search_queries(request: MusicRequest) -> list[str]:
    themes = " ".join(SEARCH_THEMES[t.upper()] for t in request.preference.themes
                      if t.upper() in SEARCH_THEMES) or "여행"
    english_themes = " ".join(SEARCH_THEMES_EN[t.upper()] for t in request.preference.themes
                              if t.upper() in SEARCH_THEMES_EN) or "travel"
    region = re.sub(r"(?:특별자치도|특별자치시|특별시|광역시|자치도|도)$", "",
                    request.region.full_name.split()[0])
    month = request.duration.local_bounds()[0].month
    korean_season, english_season = SEASONS[season_for_month(month)]
    if re.search(r"[가-힣]", region):
        queries = [
            f"{region} {korean_season} 여행 노래 공식 뮤직비디오 -vlog -playlist",
            f"{month}월 {korean_season} {themes} 여행 노래 official music video -vlog -playlist",
            f"{korean_season} 노래 official music video -vlog -playlist",
        ]
    else:
        queries = [
            f"{region} {english_season} travel song official music video -vlog -playlist",
            f"{calendar.month_name[month]} {english_season} {english_themes} travel song official music video -vlog -playlist",
            f"{english_season} song official music video -vlog -playlist",
        ]
    return [query[:240] for query in queries]


def query_text(request: MusicRequest) -> str:
    themes = ", ".join(THEME_LABELS.get(t.upper(), t) for t in request.preference.themes)
    month = request.duration.local_bounds()[0].month
    korean_season, english_season = SEASONS[season_for_month(month)]
    return (f"query: {request.region.full_name}에서 {month}월 {korean_season}({english_season})에 "
            f"{themes} 테마로 여행할 때 어울리는 노래")


class E5Encoder:
    """고정된 로컬 ONNX 모델로 마스킹 평균 풀링과 L2 정규화를 수행한다."""

    def __init__(self, model_dir: Path):
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            model_path = model_dir / "model_O4.onnx"
            tokenizer_path = model_dir / "tokenizer.json"
            if not model_path.is_file() or not tokenizer_path.is_file():
                raise FileNotFoundError("E5 model assets are missing")
            self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
            self.tokenizer.enable_truncation(max_length=512)
            self.tokenizer.enable_padding()
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            self.session = ort.InferenceSession(
                str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            self.input_names = {item.name for item in self.session.get_inputs()}
        except Exception as exc:
            raise ServiceUnavailable(reason="music_model_load_failed", detail={"error": type(exc).__name__}) from exc

    def encode(self, texts: list[str]):
        import numpy as np

        encoded = self.tokenizer.encode_batch(texts)
        ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
        mask = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)
        values = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            values["token_type_ids"] = np.asarray([item.type_ids for item in encoded], dtype=np.int64)
        hidden = self.session.run(None, values)[0]
        if hidden.ndim != 3 or hidden.shape[-1] != MODEL_DIMENSION:
            raise ValueError("unexpected E5 output shape")
        weights = mask[..., None]
        vectors = (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-12)


class E5MusicRecommender:
    def __init__(self, youtube: YouTubeMusic, model_dir: Path, *, encoder=None):
        self.youtube = youtube
        self.model_dir = model_dir
        self.encoder = encoder
        self._load_lock = asyncio.Lock()
        self._inference_slots = asyncio.Semaphore(2)

    async def _encoder(self):
        if self.encoder is None:
            async with self._load_lock:
                if self.encoder is None:
                    self.encoder = await asyncio.to_thread(E5Encoder, self.model_dir)
        return self.encoder

    async def recommend(self, request: MusicRequest) -> MusicRecommendation:
        started = time.monotonic()
        target_season = season_for_month(request.duration.local_bounds()[0].month)
        batches = await asyncio.gather(*(
            self.youtube._search(query, limit=SEARCH_LIMIT) for query in search_queries(request)
        ))
        candidates: list[SongCandidate] = []
        seen_videos: set[str] = set()
        seen_songs: set[tuple[str, str]] = set()
        for batch in batches:
            for entry in batch:
                candidate = parse_candidate(entry)
                if candidate is None:
                    continue
                seasons = mentioned_seasons(candidate.suggestion.title)
                if seasons and seasons != {target_season}:
                    continue
                video_id = candidate.entry["id"]
                song_key = (normalized(candidate.suggestion.artist), normalized(candidate.suggestion.title))
                if video_id in seen_videos or song_key in seen_songs:
                    continue
                seen_videos.add(video_id)
                seen_songs.add(song_key)
                candidates.append(candidate)
                if len(candidates) == MAX_CANDIDATES:
                    break
            if len(candidates) == MAX_CANDIDATES:
                break
        if not candidates:
            raise MusicRecommendationFailed(reason="music_candidates_unavailable")

        encoder = await self._encoder()
        try:
            async with self._inference_slots:
                vectors = await asyncio.to_thread(
                    encoder.encode, [query_text(request), *(item.passage() for item in candidates)]
                )
            scores = vectors[1:] @ vectors[0]
            order = sorted(
                range(len(candidates)),
                key=lambda index: (
                    -float(scores[index])
                    - (SEASON_MATCH_BONUS if target_season in mentioned_seasons(candidates[index].suggestion.title) else 0),
                    index,
                ),
            )
        except Exception as exc:
            raise ServiceUnavailable(reason="music_model_inference_failed", detail={"error": type(exc).__name__}) from exc
        for rank, index in enumerate(order[:VERIFY_LIMIT], 1):
            candidate = candidates[index]
            selected = await self.youtube.verify_entry(candidate.entry, candidate.suggestion)
            if selected is not None:
                record("music_recommended", season=target_season, candidates=len(candidates), rank=rank,
                       elapsed_ms=round((time.monotonic() - started) * 1000))
                return selected
        raise MusicRecommendationFailed(reason="music_video_not_verified", detail={"candidates": len(candidates)})
