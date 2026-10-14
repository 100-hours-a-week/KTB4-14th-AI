import unittest

from fastapi.testclient import TestClient

from ai_service.core.config import Settings
from ai_service.main import create_app


class MatchingOpenAPITests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(settings=Settings())
        with TestClient(self.app) as client:
            self.spec = client.get("/openapi.json").json()
        self.operation = self.spec["paths"]["/api/ai/v1/matches/candidates"]["post"]

    def test_mock_path_is_removed_and_backend_matching_remains_visible(self):
        self.assertFalse(any(route.path == "/matching-requests" for route in self.app.routes))
        self.assertNotIn("/internal/ai/matching/recommend", self.spec["paths"])
        self.assertNotIn("/matching-requests", self.spec["paths"])
        self.assertNotIn("/api/ai/v1/traveler-match-jobs", self.spec["paths"])
        self.assertNotIn("202", self.operation["responses"])
        self.assertNotIn("Idempotency-Key", [p["name"] for p in self.operation.get("parameters", [])])
        self.assertEqual(self.operation["operationId"], "matching_candidates_api_ai_v1_matches_candidates_post")
        self.assertEqual(self.operation["tags"], ["V2"])

    def test_single_bearer_scheme_is_used(self):
        self.assertEqual(self.operation["security"], [{"HTTPBearer": []}])
        self.assertEqual(set(self.spec["components"]["securitySchemes"]), {"HTTPBearer"})

    def test_removed_demo_returns_404_and_has_no_runtime_services(self):
        with TestClient(self.app) as client:
            for headers in ({}, {"Authorization": "Bearer test-only"}):
                response = client.post("/matching-requests", json={}, headers=headers)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json(), {"message": "not_found", "data": None})
            self.assertFalse(hasattr(self.app.state, "matching_recommender"))
            self.assertFalse(hasattr(self.app.state, "matching_candidate_source"))


if __name__ == "__main__":
    unittest.main()
