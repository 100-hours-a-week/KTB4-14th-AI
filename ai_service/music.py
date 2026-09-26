"""Resolve an actual YouTube music video without an API key or media download."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
import html
import json
import re
import sys
import time
import unicodedata
from urllib.parse import urlencode

import httpx

from ai_service.errors import ServiceUnavailable
from ai_service.schemas import MusicRecommendation, MusicSuggestion


def normalized(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", html.unescape(value)).casefold() if c.isalnum())


def matches_song(title: str, author: str, suggestion: MusicSuggestion) -> bool:
    """Conservative metadata matching, not proof of an official rights holder."""
    # Word boundaries avoid treating Yellow/봄 as Yellowstone/봄날.
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", html.unescape(suggestion.title)).casefold())
    pattern = r"(?<!\w)" + r"[\W_]*".join(re.escape(token) for token in tokens) + r"(?!\w)"
    if not tokens or not re.search(pattern, unicodedata.normalize("NFKC", html.unescape(title)).casefold()):
        return False
    if normalized(suggestion.artist) not in normalized(title + " " + author):
        return False
    variants = r"\b(?:cover|live|remix|karaoke|reaction|instrumental|slowed|sped\s*up)\b|커버|라이브|노래방"
    requested = suggestion.title + " " + suggestion.artist
    return not any(normalized(word) not in normalized(requested)
                   for word in re.findall(variants, title, re.IGNORECASE))


def fallback_music() -> MusicRecommendation:
    # Music is an extra: an unverifiable pick must not fail an otherwise complete trip.
    return MusicRecommendation(
        title="여행을 떠나요",
        artist="조용필",
        youtube_url="https://www.youtube.com/results?"
        + urlencode({"search_query": "조용필 여행을 떠나요 official audio"}),
    )


class YouTubeMusic:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.cache: OrderedDict[tuple[str, str], tuple[float, MusicRecommendation]] = OrderedDict()
        self.search_slots = asyncio.Semaphore(2)

    async def _search(self, query: str) -> list[dict]:
        # A subprocess allows SSE disconnect/timeout to stop extraction immediately.
        # Flat metadata only: no video/audio downloads, login, cookies or user config.
        async with self.search_slots:
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-cache-dir",
                    "--flat-playlist", "--skip-download", "--dump-single-json", "--no-warnings",
                    "--socket-timeout", "8", "--retries", "0", "--extractor-retries", "0",
                    "--", "ytsearch5:" + query,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError as exc:
                raise ServiceUnavailable(reason="youtube_search_launch_failed", detail={"error": type(exc).__name__}) from exc
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20)
                if process.returncode:
                    raise ServiceUnavailable(reason="youtube_search_failed", detail={"returncode": process.returncode})
                payload = json.loads(stdout)
                entries = payload.get("entries") if isinstance(payload, dict) else None
                if not isinstance(entries, list):
                    raise ValueError("invalid YouTube search response")
                return entries[:5]
            except (TimeoutError, ValueError) as exc:
                raise ServiceUnavailable(reason="youtube_search_timeout_or_invalid", detail={"error": type(exc).__name__}) from exc
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()

    async def find_video(self, suggestion: MusicSuggestion) -> MusicRecommendation | None:
        key = (normalized(suggestion.title), normalized(suggestion.artist))
        if not all(key):
            return None
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(key)
            return cached[1].model_copy(deep=True)
        entries = await self._search(f"{suggestion.artist} {suggestion.title} official audio")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video_id, title = entry.get("id"), entry.get("title")
            author = entry.get("channel") or entry.get("uploader") or ""
            if (not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
                    or not isinstance(title, str) or not isinstance(author, str)
                    or entry.get("live_status") in ("is_live", "is_upcoming")
                    or not matches_song(title, author, suggestion)):
                continue
            # Construct only from a real search result ID, never from model output/URL.
            url = "https://www.youtube.com/watch?v=" + video_id
            try:
                response = await self.client.get(
                    "https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=8.0,
                )
                if response.status_code in {401, 403, 404, 410}:
                    continue  # Try another result, without bypassing restrictions.
                response.raise_for_status()
                metadata = response.json()
                if not isinstance(metadata, dict):
                    raise ValueError("invalid YouTube metadata")
            except (httpx.HTTPError, ValueError) as exc:
                raise ServiceUnavailable(reason="youtube_oembed_failed", detail={"error": type(exc).__name__, "http_status": getattr(getattr(exc, "response", None), "status_code", None)}) from exc
            verified_title, verified_author = metadata.get("title"), metadata.get("author_name")
            if (metadata.get("type") != "video" or not isinstance(verified_title, str)
                    or not isinstance(verified_author, str)
                    or not matches_song(verified_title, verified_author, suggestion)):
                continue
            song = MusicRecommendation(title=suggestion.title, artist=suggestion.artist, youtube_url=url)
            self.cache[key] = (time.monotonic() + 600, song)
            self.cache.move_to_end(key)
            if len(self.cache) > 128:
                self.cache.popitem(last=False)
            return song.model_copy(deep=True)
        return None
