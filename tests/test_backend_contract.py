import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from ai_service.config import Settings
from ai_service.errors import MusicRecommendationFailed
from ai_service.main import create_app
from test_compact_routes import PlaceClient, Planner
from test_day_transport import http_request, request


PATH = "/internal/ai/itineraries/generate"
REMOVED_PATH = "/internal/ai/itineraries/generate"
HEADERS = {"Authorization": "Bearer test-only"}


class BackendContractTests(unittest.TestCase):
    def app(self):
        return create_app(settings=Settings(
            api_token="test-only", openai_api_key="test", kakao_rest_api_key="test",
        ))

    def test_backend_receives_legacy_json_after_all_four_stages(self):
        calls = []

        class TracedPlaces(PlaceClient):
            async def collect(self, *args, **kwargs):
                calls.append("PLACE_RECOMMEND")
                return await super().collect(*args, **kwargs)

            async def accommodations(self, *args):
                calls.append("STAY_RECOMMEND")
                return await super().accommodations(*args)

        class TracedPlanner(Planner):
            async def recommend_music(self, body):
                calls.append("MUSIC_RECOMMEND")
                return await super().recommend_music(body)

        app = self.app()
        with TestClient(app) as client:
            app.state.places, app.state.planner = TracedPlaces(), TracedPlanner()
            original = app.state.routes.route

            async def route(*args):
                if "ROUTE_OPTIMIZE" not in calls:
                    calls.append("ROUTE_OPTIMIZE")
                return await original(*args)

            app.state.routes.route = route
            with self.assertLogs("uvicorn.error.audigo", level="INFO") as logs:
                response = client.post(PATH, json=http_request(request(None, "CAR")), headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, ["PLACE_RECOMMEND", "STAY_RECOMMEND", "ROUTE_OPTIMIZE", "MUSIC_RECOMMEND"])
        stages = [entry for line in logs.output
                  if (entry := json.loads(line.split(":", 2)[2])).get("event") == "generation_stage"]
        self.assertEqual([(entry["stage"], entry["status"]) for entry in stages], [
            (stage, status)
            for stage in calls for status in ("STARTED", "COMPLETED")
        ])
        self.assertTrue(all(entry["request_id"] == response.headers["x-request-id"] for entry in stages))
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.json()["travel_plan_id"], 10)
        self.assertIn("days", response.json())
        self.assertNotIn("music", response.json())

    def test_music_failure_falls_back_before_json_response(self):
        class FailingPlanner(Planner):
            async def recommend_music(self, body):
                raise MusicRecommendationFailed()

        app = self.app()
        with self.assertLogs("uvicorn.error.audigo", level="ERROR"), TestClient(app) as client:
            app.state.places, app.state.planner = PlaceClient(), FailingPlanner()
            response = client.post(PATH, json=http_request(request(None, "CAR")), headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("days", response.json())

    def test_auth_and_removed_stream_path(self):
        with TestClient(self.app()) as client:
            self.assertEqual(client.post(PATH, json=http_request()).status_code, 401)
            self.assertEqual(client.post(PATH, json=http_request(), headers={"Authorization": "Bearer wrong"}).status_code, 401)
            self.assertEqual(client.post(REMOVED_PATH, json=http_request(), headers=HEADERS).status_code, 404)
            paths = client.get("/openapi.json").json()["paths"]
            self.assertEqual({path for path, methods in paths.items() if "post" in methods}, {
                PATH, "/internal/ai/music/recommend",
            })

    def test_unexpected_failure_keeps_existing_500_shape(self):
        with patch("ai_service.main.generate_plan", AsyncMock(side_effect=RuntimeError("provider secret"))), \
                TestClient(self.app(), raise_server_exceptions=False) as client:
            response = client.post(PATH, json=http_request(request(None, "CAR")), headers=HEADERS)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"message": "internal_server_error", "data": None})
        self.assertNotIn("provider secret", response.text)

    def test_generation_timeout_keeps_existing_503_shape(self):
        with patch("ai_service.main.generate_plan", AsyncMock(side_effect=TimeoutError())), \
                TestClient(self.app()) as client:
            response = client.post(PATH, json=http_request(request(None, "CAR")), headers=HEADERS)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            "message": "ai_service_unavailable",
            "data": {"error_message": "잠시 후 다시 시도해주세요."},
        })
