"""E5 후보 선택과 실제 모델 실행의 회귀 검사."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

import numpy as np

from ai_service.e5_music import (
    E5Encoder, E5MusicRecommender, mentioned_seasons, parse_candidate,
    query_text, search_queries, season_for_month,
)
from ai_service.errors import MusicRecommendationFailed, ServiceUnavailable
from ai_service.schemas import MusicRecommendation, MusicRequest
from ai_service.api_examples import MUSIC_REQUEST_EXAMPLE


REQUEST = MusicRequest.model_validate(MUSIC_REQUEST_EXAMPLE)
MODEL_DIR = Path(__file__).resolve().parents[1] / "model"


def request_for_month(month):
    payload = deepcopy(MUSIC_REQUEST_EXAMPLE)
    payload["duration"] = {
        "arrival_datetime": f"2026-{month:02d}-15T13:00:00",
        "departure_datetime": f"2026-{month:02d}-17T18:00:00",
    }
    return MusicRequest.model_validate(payload)


def video(video_id, title, channel):
    return {"id": video_id, "title": title, "channel": channel}


class FakeEncoder:
    def __init__(self):
        self.texts = []

    def encode(self, texts):
        self.texts = texts
        # 첫 후보보다 두 번째 후보가 여행 조건에 더 가깝다.
        return np.asarray([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]], dtype=np.float32)


class E5MusicTests(unittest.IsolatedAsyncioTestCase):
    def youtube(self, batches, verified):
        youtube = type("FakeYouTube", (), {})()
        youtube._search = AsyncMock(side_effect=batches)
        youtube.verify_entry = AsyncMock(side_effect=verified)
        return youtube

    async def test_ranks_deduplicated_candidates_then_verifies_next_video(self):
        first = video("aaaaaaaaaaa", "Coldplay - Yellow (Official Video)", "Coldplay")
        second = video("bbbbbbbbbbb", "TWICE - Dance The Night Away (Official Video)", "TWICE")
        youtube = self.youtube([
            [first, first, video("ccccccccccc", "Chill travel playlist", "Channel")],
            [second, video("ddddddddddd", "TWICE - Dance The Night Away (Live)", "TWICE")],
            [first],
        ], [None, MusicRecommendation(title="Yellow", artist="Coldplay",
                                      youtube_url="https://www.youtube.com/watch?v=aaaaaaaaaaa")])
        encoder = FakeEncoder()
        selected = await E5MusicRecommender(youtube, MODEL_DIR, encoder=encoder).recommend(REQUEST)
        self.assertEqual(selected.title, "Yellow")
        self.assertEqual([call.args[0]["id"] for call in youtube.verify_entry.await_args_list],
                         ["bbbbbbbbbbb", "aaaaaaaaaaa"])
        self.assertEqual(youtube._search.await_count, 3)
        self.assertTrue(all(call.kwargs["limit"] == 10 for call in youtube._search.await_args_list))
        self.assertTrue(encoder.texts[0].startswith("query: "))
        self.assertTrue(all(text.startswith("passage: ") for text in encoder.texts[1:]))
        self.assertEqual(len(encoder.texts), 3)

    async def test_no_valid_candidates_is_422(self):
        youtube = self.youtube([[video("aaaaaaaaaaa", "Travel playlist", "DJ")], [], []], [])
        with self.assertRaises(MusicRecommendationFailed):
            await E5MusicRecommender(youtube, MODEL_DIR, encoder=FakeEncoder()).recommend(REQUEST)
        youtube.verify_entry.assert_not_called()

    async def test_all_unverified_candidates_are_422(self):
        candidate = video("aaaaaaaaaaa", "Coldplay - Yellow", "Coldplay")
        youtube = self.youtube([[candidate], [], []], [None])
        class OneCandidateEncoder:
            def encode(self, texts):
                return np.asarray([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)
        with self.assertRaises(MusicRecommendationFailed):
            await E5MusicRecommender(youtube, MODEL_DIR, encoder=OneCandidateEncoder()).recommend(REQUEST)
        youtube.verify_entry.assert_awaited_once()

    async def test_wrong_season_is_excluded_and_matching_season_gets_priority(self):
        winter = video("aaaaaaaaaaa", "Ludy - 겨울 여행의 시작", "Ludy")
        neutral = video("bbbbbbbbbbb", "Coldplay - Yellow", "Coldplay")
        summer = video("ccccccccccc", "Joje - 여름 제주", "Joje")
        youtube = self.youtube([[winter, neutral], [summer], []], [
            MusicRecommendation(title="여름 제주", artist="Joje",
                                youtube_url="https://www.youtube.com/watch?v=ccccccccccc"),
        ])
        class SeasonEncoder:
            def __init__(self):
                self.texts = []

            def encode(self, texts):
                self.texts = texts
                # 계절 보너스가 없으면 중립 곡이 더 높은 유사도다.
                return np.asarray([[1.0, 0.0], [0.90, 0.0], [0.89, 0.0]], dtype=np.float32)

        encoder = SeasonEncoder()
        result = await E5MusicRecommender(youtube, MODEL_DIR, encoder=encoder).recommend(REQUEST)
        self.assertEqual(result.title, "여름 제주")
        self.assertFalse(any("겨울 여행의 시작" in text for text in encoder.texts))
        self.assertEqual(youtube.verify_entry.await_args.args[0]["id"], "ccccccccccc")

    async def test_missing_model_and_search_failure_are_503(self):
        candidate = video("aaaaaaaaaaa", "Coldplay - Yellow", "Coldplay")
        youtube = self.youtube([[candidate], [], []], [])
        with self.assertRaises(ServiceUnavailable) as missing:
            await E5MusicRecommender(youtube, Path("/missing/e5/model")).recommend(REQUEST)
        self.assertEqual(missing.exception.status_code, 503)

        youtube = self.youtube([ServiceUnavailable(reason="youtube_search_failed"), [], []], [])
        with self.assertRaises(ServiceUnavailable):
            await E5MusicRecommender(youtube, MODEL_DIR, encoder=FakeEncoder()).recommend(REQUEST)

    def test_candidate_filters_and_context(self):
        for title in (
            "Coldplay - Yellow (Live)", "Travel Playlist", "Coldplay - Yellow Cover",
            "Top 7 Seafood Experiences in Australia - Gourmet Adventures | Tourism Australia",
            "[MV in 제주] 끝이라고 말할 것 같았어 - 황치열",
            "🎵 BONGI - 성산포 (제주도 여행 MV) | BONGI (AI Lia) Music",
            "자연의 소리 - 힘들 때 듣는 겨울의 파도 소리",
            "YTN - [영상] 가을녘, 황혼의 조화",
        ):
            self.assertIsNone(parse_candidate(video("aaaaaaaaaaa", title, "Coldplay")))
        self.assertEqual(parse_candidate(video("aaaaaaaaaaa", "Coldplay - Yellow (Official Video)", "Coldplay")).suggestion.title, "Yellow")
        topic = parse_candidate(video("aaaaaaaaaaa", "제주도의 푸른 밤", "성시경 - Topic"))
        self.assertEqual((topic.suggestion.title, topic.suggestion.artist), ("제주도의 푸른 밤", "성시경"))
        self.assertEqual(parse_candidate(video("aaaaaaaaaaa", "MV l Joje (조제) - 너와, 제주", "DanalEntertainment")).suggestion.artist, "Joje (조제)")
        self.assertIn("제주", search_queries(REQUEST)[0])
        self.assertNotIn("음식", " ".join(search_queries(REQUEST)))
        self.assertIn("8월", query_text(REQUEST))
        self.assertIn("여름", query_text(REQUEST))
        english = REQUEST.model_copy(update={"region": REQUEST.region.model_copy(update={"full_name": "Tokyo"})})
        self.assertIn("Tokyo", search_queries(english)[0])
        self.assertIn("August", search_queries(english)[1])

    def test_months_map_to_expected_seasons(self):
        expected = {
            "SPRING": (3, 4, 5), "SUMMER": (6, 7, 8),
            "AUTUMN": (9, 10, 11), "WINTER": (12, 1, 2),
        }
        labels = {"SPRING": "봄", "SUMMER": "여름", "AUTUMN": "가을", "WINTER": "겨울"}
        for season, months in expected.items():
            for month in months:
                with self.subTest(month=month):
                    request = request_for_month(month)
                    self.assertEqual(season_for_month(month), season)
                    self.assertIn(labels[season], query_text(request))
                    self.assertTrue(all(labels[season] in query for query in search_queries(request)))
        self.assertEqual(mentioned_seasons("Winter Travel / 겨울 여행"), {"WINTER"})
        self.assertEqual(mentioned_seasons("Spring Day"), {"SPRING"})
        self.assertEqual(mentioned_seasons("Fall in Love"), set())

    def test_season_uses_korean_local_arrival_date(self):
        payload = deepcopy(MUSIC_REQUEST_EXAMPLE)
        payload["duration"] = {
            "arrival_datetime": "2026-02-28T15:30:00+00:00",
            "departure_datetime": "2026-03-02T00:00:00+00:00",
        }
        request = MusicRequest.model_validate(payload)
        self.assertIn("3월 봄", query_text(request))
        self.assertTrue(all("봄" in query for query in search_queries(request)))


class E5ModelSmokeTests(unittest.TestCase):
    @unittest.skipUnless((MODEL_DIR / "model_O4.onnx").exists() and (MODEL_DIR / "tokenizer.json").exists(),
                         "download the pinned model with scripts/download_e5_model.py")
    def test_real_korean_and_english_embeddings(self):
        encoder = E5Encoder(MODEL_DIR)
        vectors = encoder.encode([
            "query: 제주 바다 여행에 어울리는 음악",
            "passage: 제주 바다 여행 노래. 가수 테스트.",
            "passage: Music for a city walk. Artist Test.",
        ])
        self.assertEqual(vectors.shape, (3, 384))
        self.assertTrue(np.isfinite(vectors).all())
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
        self.assertGreater(float(vectors[1] @ vectors[0]), float(vectors[2] @ vectors[0]))
