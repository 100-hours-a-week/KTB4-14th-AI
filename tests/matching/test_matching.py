"""백엔드·큐에서 공유하는 E5 추천 엔진의 필터·정렬·오류를 검증한다."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from ai_service.core.errors import ServiceUnavailable
from ai_service.matching.schemas import MatchingRecommendationRequest
from ai_service.matching.service import E5MatchingRecommender, eligible_candidates


MODEL_DIR = Path(__file__).resolve().parents[2] / "model"
RANKING_FIXTURE = json.loads((Path(__file__).with_name("fixtures") / "ranking.json").read_text())


class RankingEncoder:
    def __init__(self):
        self.texts = []

    def encode(self, texts):
        self.texts = texts
        # 후보 3과 7은 같은 점수이며 후보 2는 더 낮은 점수다.
        return np.asarray([[1, 0], [0.6, 0.8], [0.8, 0.6], [0.8, 0.6]], dtype=np.float32)


class MatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_before_encoding_and_ranks_with_stable_ties(self):
        encoder = RankingEncoder()
        body = deepcopy(RANKING_FIXTURE)
        body["top_k"] = 2
        recommender = E5MatchingRecommender(MODEL_DIR, encoder=encoder)
        response = await recommender.recommend(MatchingRecommendationRequest.model_validate(body))
        self.assertEqual(response.message, "ai_companions_recommended")
        self.assertEqual(response.data.requester_id, 1)
        self.assertEqual([item.user_id for item in response.data.recommendations], [3, 7])
        self.assertEqual([item.rank for item in response.data.recommendations], [1, 2])
        self.assertAlmostEqual(response.data.recommendations[0].score, 0.8)
        self.assertEqual(len(encoder.texts), 4)
        self.assertTrue(encoder.texts[0].startswith("query: "))
        self.assertTrue(all(text.startswith("passage: ") for text in encoder.texts[1:]))
        self.assertIn("자연", encoder.texts[0])
        self.assertIn("음식", encoder.texts[0])
        self.assertNotIn("성별과 다릅니다", " ".join(encoder.texts))

    def test_invalid_internal_inputs_are_rejected(self):
        mutations = (
            lambda body: body.pop("candidates"),
            lambda body: body.update(budget_min=4000000),
            lambda body: body.update(top_k=0),
            lambda body: body.update(requester_id=True),
            lambda body: body.update(pace="unknown"),
            lambda body: body.update(theme=[]),
            lambda body: body.update(user_id=99),
            lambda body: body["candidates"].append(deepcopy(body["candidates"][0])),
            lambda body: body["candidates"][1].update(budget_max=-1),
            lambda body: body.update(candidates=body["candidates"] * 26),
        )
        for mutate in mutations:
            body = deepcopy(RANKING_FIXTURE)
            mutate(body)
            with self.subTest(body_keys=list(body)), self.assertRaises(ValueError):
                MatchingRecommendationRequest.model_validate(body)

    async def test_empty_or_fully_filtered_candidates_do_not_load_model(self):
        recommender = E5MatchingRecommender(Path("/missing/e5/model"))
        for candidates in ([], [RANKING_FIXTURE["candidates"][0]], [RANKING_FIXTURE["candidates"][3]]):
            body = MatchingRecommendationRequest.model_validate({**RANKING_FIXTURE, "candidates": candidates})
            response = await recommender.recommend(body)
            self.assertEqual(response.data.model_dump(), {"requester_id": 1, "recommendations": []})
        self.assertIsNone(recommender.encoder)

    def test_budget_boundaries_and_missing_budget_policy(self):
        body = deepcopy(RANKING_FIXTURE)
        body["candidates"][4].update(budget_max=100000)
        body["candidates"][5].update(budget_min=3000000)
        request = MatchingRecommendationRequest.model_validate(body)
        self.assertEqual([candidate.user_id for candidate in eligible_candidates(request)], [2, 3, 5, 6, 7])
        body.update(budget_min=None, budget_max=None, preferred_companion_gender="Any")
        request = MatchingRecommendationRequest.model_validate(body)
        self.assertEqual([candidate.user_id for candidate in eligible_candidates(request)], [2, 3, 4, 5, 6, 7, 8])

    def test_normalizes_backend_values(self):
        body = deepcopy(RANKING_FIXTURE)
        body.update(theme=["NATURE", " nature ", "FOOD"], pace="BALANCED", preferred_companion_gender="female")
        request = MatchingRecommendationRequest.model_validate(body)
        self.assertEqual((request.theme, request.pace, request.preferred_companion_gender),
                         (["nature", "food"], "Balanced", "Female"))

    async def test_missing_model_and_invalid_vectors_raise_service_unavailable(self):
        body = MatchingRecommendationRequest.model_validate(RANKING_FIXTURE)
        with TemporaryDirectory() as directory:
            with self.assertRaises(ServiceUnavailable) as caught:
                await E5MatchingRecommender(Path(directory)).recommend(body)
            self.assertEqual(caught.exception.status_code, 503)

        class BrokenEncoder:
            def encode(self, texts):
                return np.full((len(texts), 3), np.nan)

        with self.assertRaises(ServiceUnavailable) as caught:
            await E5MatchingRecommender(MODEL_DIR, encoder=BrokenEncoder()).recommend(body)
        self.assertEqual(caught.exception.status_code, 503)


class RealMatchingE5Tests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless((MODEL_DIR / "model_O4.onnx").is_file() and (MODEL_DIR / "tokenizer.json").is_file(),
                         "local pinned E5 model required")
    async def test_real_e5_ranks_candidates(self):
        recommender = E5MatchingRecommender(MODEL_DIR)
        response = await recommender.recommend(MatchingRecommendationRequest.model_validate(RANKING_FIXTURE))
        recommendations = response.data.recommendations
        self.assertEqual({item.user_id for item in recommendations}, {2, 3, 7})
        self.assertEqual([item.rank for item in recommendations], [1, 2, 3])
        scores = [item.score for item in recommendations]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(np.isfinite(score) and -1 <= score <= 1 for score in scores))
        self.assertIsNotNone(recommender.encoder)


if __name__ == "__main__":
    unittest.main()
