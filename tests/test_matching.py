"""동행자 조건 필터, 실제 API 처리, 로컬 E5 실행을 검증한다."""

from copy import deepcopy
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

import httpx
import numpy as np
from fastapi.testclient import TestClient

from ai_service.config import Settings
from ai_service.main import create_app
from ai_service.matching import MatchingRecommendationRequest, MatchingRequestCreate, eligible_candidates
from ai_service.matching_examples import MATCHING_CREATE_EXAMPLE, MATCHING_RECOMMENDATION_EXAMPLE


PATH = "/matching-requests"
HEADERS = {"Authorization": "Bearer test-only"}
MODEL_DIR = Path(__file__).resolve().parents[1] / "model"


def block_network(request):
    raise AssertionError("matching must not call providers")


class RankingEncoder:
    def __init__(self):
        self.texts = []

    def encode(self, texts):
        self.texts = texts
        # 후보 3과 7은 같은 점수이며 후보 2는 더 낮은 점수다.
        return np.asarray([[1, 0], [0.6, 0.8], [0.8, 0.6], [0.8, 0.6]], dtype=np.float32)


class MatchingTests(unittest.TestCase):
    def app(self, **settings):
        return create_app(settings=Settings(api_token="test-only", **settings),
                          transport=httpx.MockTransport(block_network))

    def test_filters_before_encoding_and_ranks_with_stable_ties(self):
        app = self.app()
        encoder = RankingEncoder()
        body = deepcopy(MATCHING_RECOMMENDATION_EXAMPLE)
        body["top_k"] = 2
        with TestClient(app) as client:
            app.state.matching_recommender.encoder = encoder
            app.state.matching_candidate_source.context = MatchingRecommendationRequest.model_validate(body)
            response = client.post(PATH, json=MATCHING_CREATE_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["message"], "ai_companions_recommended")
        data = response.json()["data"]
        self.assertEqual(data["requester_id"], 1)
        self.assertEqual([item["user_id"] for item in data["recommendations"]], [3, 7])
        self.assertEqual([item["rank"] for item in data["recommendations"]], [1, 2])
        self.assertAlmostEqual(data["recommendations"][0]["score"], 0.8)
        self.assertEqual(len(encoder.texts), 4)
        self.assertTrue(encoder.texts[0].startswith("query: "))
        self.assertTrue(all(text.startswith("passage: ") for text in encoder.texts[1:]))
        self.assertIn("자연", encoder.texts[0])
        self.assertIn("음식", encoder.texts[0])
        self.assertNotIn("성별과 다릅니다", " ".join(encoder.texts))

    def test_auth_is_required_and_not_confused_with_requester_id(self):
        with TestClient(self.app()) as client:
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                response = client.post(PATH, json=MATCHING_CREATE_EXAMPLE, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"message": "unauthorized", "data": None})

    def test_invalid_contracts_return_400(self):
        mutations = (
            lambda body: body.pop("preferred_companion_gender"),
            lambda body: body.pop("theme"),
            lambda body: body.pop("pace"),
            lambda body: body.update(preferred_companion_gender=None),
            lambda body: body.update(theme=None),
            lambda body: body.update(pace=None),
            lambda body: body.update(budget_min=4000000),
            lambda body: body.update(top_k=0),
            lambda body: body.update(requester_id=True),
            lambda body: body.update(pace="unknown"),
            lambda body: body.update(theme=[]),
            lambda body: body.update(user_id=99),
            lambda body: body.update(candidates=[]),
            lambda body: body.update(introduction="요청 메시지"),
            lambda body: body.update(buget_max=3000000),
            lambda body: body.update(budget_max=-1),
        )
        with TestClient(self.app()) as client:
            for mutate in mutations:
                body = deepcopy(MATCHING_CREATE_EXAMPLE)
                mutate(body)
                with self.subTest(body_keys=list(body)):
                    response = client.post(PATH, json=body, headers=HEADERS)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json()["message"], "invalid_request")
            response = client.post(PATH, content="{broken", headers={**HEADERS, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 400)

    def test_empty_or_fully_filtered_candidates_do_not_load_model(self):
        app = self.app(e5_model_dir=Path("/missing/e5/model"))
        with TestClient(app) as client:
            for candidates in ([], [MATCHING_RECOMMENDATION_EXAMPLE["candidates"][0]],
                               [MATCHING_RECOMMENDATION_EXAMPLE["candidates"][3]]):
                app.state.matching_candidate_source.context = MatchingRecommendationRequest.model_validate(
                    {**MATCHING_RECOMMENDATION_EXAMPLE, "candidates": candidates}
                )
                response = client.post(PATH, json=MATCHING_CREATE_EXAMPLE, headers=HEADERS)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["data"], {"requester_id": 1, "recommendations": []})

    def test_budget_boundaries_and_missing_budget_policy(self):
        body = deepcopy(MATCHING_RECOMMENDATION_EXAMPLE)
        body["candidates"][4].update(budget_max=100000)
        body["candidates"][5].update(budget_min=3000000)
        request = MatchingRecommendationRequest.model_validate(body)
        self.assertEqual([candidate.user_id for candidate in eligible_candidates(request)], [2, 3, 5, 6, 7])
        body.update(budget_min=None, budget_max=None, preferred_companion_gender="Any")
        request = MatchingRecommendationRequest.model_validate(body)
        self.assertEqual([candidate.user_id for candidate in eligible_candidates(request)], [2, 3, 4, 5, 6, 7, 8])

    def test_normalizes_backend_values(self):
        body = deepcopy(MATCHING_RECOMMENDATION_EXAMPLE)
        body.update(theme=["NATURE", " nature ", "FOOD"], pace="BALANCED", preferred_companion_gender="female")
        request = MatchingRecommendationRequest.model_validate(body)
        self.assertEqual((request.theme, request.pace, request.preferred_companion_gender),
                         (["nature", "food"], "Balanced", "Female"))

    def test_missing_model_and_invalid_vectors_are_503_without_fabricated_results(self):
        with TemporaryDirectory() as directory, TestClient(self.app(e5_model_dir=Path(directory))) as client:
            response = client.post(PATH, json=MATCHING_CREATE_EXAMPLE, headers=HEADERS)
            self.assertEqual(response.status_code, 503)
        class BrokenEncoder:
            def encode(self, texts):
                return np.full((len(texts), 3), np.nan)
        app = self.app()
        with TestClient(app) as client:
            app.state.matching_recommender.encoder = BrokenEncoder()
            response = client.post(PATH, json=MATCHING_CREATE_EXAMPLE, headers=HEADERS)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("recommendations", response.json()["data"])

    def test_swagger_has_executable_mock_request_and_single_auth(self):
        with TestClient(self.app()) as client:
            schema = client.get("/openapi.json").json()
            self.assertEqual(client.get("/docs").status_code, 200)
        operation = schema["paths"][PATH]["post"]
        self.assertEqual(operation["security"], [{"HTTPBearer": []}])
        self.assertEqual(set(schema["components"]["securitySchemes"]), {"HTTPBearer"})
        self.assertEqual(set(operation["responses"]), {"200", "400", "401", "500", "503"})
        shown = operation["requestBody"]["content"]["application/json"]["examples"]["mock"]["value"]
        self.assertEqual(shown, MATCHING_CREATE_EXAMPLE)
        MatchingRequestCreate.model_validate(shown)

    def test_optional_budgets_are_not_required_and_preferences_reach_e5(self):
        app = self.app()
        with TestClient(app) as client:
            app.state.matching_recommender.recommend = AsyncMock(return_value={
                "message": "ai_companions_recommended",
                "data": {"requester_id": 1, "recommendations": []},
            })
            body = {"preferred_companion_gender": "Any", "theme": ["relax"], "pace": "Relaxed"}
            response = client.post(PATH, json=body, headers=HEADERS)
            self.assertEqual(response.status_code, 200, response.text)
            received = app.state.matching_recommender.recommend.await_args.args[0]
            self.assertEqual(received.preferred_companion_gender, "Any")
            self.assertEqual(received.theme, ["relax"])
            self.assertEqual(received.pace, "Relaxed")
            self.assertIsNone(received.budget_min)
            self.assertIsNone(received.budget_max)
            self.assertEqual(received.requester_id, 1)
            self.assertEqual(len(received.candidates), 8)

    def test_internal_candidate_contract_still_validates_duplicate_ids(self):
        body = deepcopy(MATCHING_RECOMMENDATION_EXAMPLE)
        body["candidates"].append(deepcopy(body["candidates"][0]))
        with self.assertRaises(ValueError):
            MatchingRecommendationRequest.model_validate(body)

    def test_handler_timeout_returns_503(self):
        async def slow_recommend(body):
            await asyncio.sleep(1)
        app = self.app(model_timeout_seconds=0.01)
        with TestClient(app) as client:
            app.state.matching_recommender.recommend = AsyncMock(side_effect=slow_recommend)
            response = client.post(PATH, json=MATCHING_CREATE_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["message"], "ai_service_unavailable")


class RealMatchingE5Tests(unittest.TestCase):
    @unittest.skipUnless((MODEL_DIR / "model_O4.onnx").is_file() and (MODEL_DIR / "tokenizer.json").is_file(),
                         "local pinned E5 model required")
    def test_mock_request_runs_real_e5_over_http_without_provider_keys(self):
        app = create_app(settings=Settings(api_token="test-only", e5_model_dir=MODEL_DIR),
                         transport=httpx.MockTransport(block_network))
        with TestClient(app) as client:
            response = client.post(PATH, json=MATCHING_CREATE_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        recommendations = response.json()["data"]["recommendations"]
        self.assertEqual({item["user_id"] for item in recommendations}, {2, 3, 7})
        self.assertEqual([item["rank"] for item in recommendations], [1, 2, 3])
        scores = [item["score"] for item in recommendations]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(np.isfinite(score) and -1 <= score <= 1 for score in scores))
        self.assertIsNotNone(app.state.matching_recommender.encoder)


if __name__ == "__main__":
    unittest.main()
