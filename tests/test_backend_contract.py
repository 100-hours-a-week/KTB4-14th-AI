import json
import unittest

from fastapi.testclient import TestClient

from ai_service.config import Settings
from ai_service.errors import MusicRecommendationFailed
from ai_service.main import create_app
from test_compact_routes import PlaceClient, Planner
from test_day_transport import http_request, request


PATH = "/api/ai/v1/itinerary-jobs/stream"
HEADERS = {"Authorization": "Bearer test-only"}


def events(text):
    return [
        (block.split("event: ", 1)[1].splitlines()[0],
         json.loads(block.split("data: ", 1)[1]))
        for block in text.split("\n\n") if "data: " in block
    ]


class BackendContractTests(unittest.TestCase):
    def app(self):
        return create_app(settings=Settings(
            api_token="test-only", openai_api_key="test", kakao_rest_api_key="test",
        ))

    def test_backend_can_finish_and_find_result_when_required_stages_are_done(self):
        app = self.app()
        with TestClient(app) as client:
            app.state.places, app.state.planner = PlaceClient(), Planner()
            response = client.post(PATH, json=http_request(request(None, "CAR")), headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        received = events(response.text)
        self.assertEqual([name for name, _ in received], [
            "PLACE_RECOMMEND_STARTED", "PLACE_RECOMMEND_DONE",
            "STAY_RECOMMEND_STARTED", "STAY_RECOMMEND_DONE",
            "ROUTE_OPTIMIZE_STARTED", "MUSIC_RECOMMEND_STARTED",
            "MUSIC_RECOMMEND_DONE", "ROUTE_OPTIMIZE_DONE", "complete",
        ])
        # The Java service commits as soon as these three stages are DONE.
        # Its persistence reader finds result.days in the stored ROUTE payload.
        done, payloads = set(), {}
        for name, payload in received:
            if name.endswith("_DONE"):
                done.add(payload["stage"])
                payloads[payload["stage"]] = payload
            if {"PLACE_RECOMMEND", "STAY_RECOMMEND", "ROUTE_OPTIMIZE"} <= done:
                result = payloads["ROUTE_OPTIMIZE"]["result"]
                self.assertGreater(len(result["days"]), 0)
                self.assertIn("MUSIC_RECOMMEND", done)
                self.assertEqual(result["music"]["title"], "테스트 음악")
        self.assertEqual(sum("result" in value for _, value in received), 1)

    def test_music_failure_falls_back_and_still_completes(self):
        class FailingPlanner(Planner):
            async def recommend_music(self, body):
                raise MusicRecommendationFailed()

        app = self.app()
        with self.assertLogs("uvicorn.error.audigo", level="ERROR"), TestClient(app) as client:
            app.state.places, app.state.planner = PlaceClient(), FailingPlanner()
            response = client.post(PATH, json=http_request(request(None, "CAR")), headers=HEADERS)
        received = events(response.text)
        names = [name for name, _ in received]
        self.assertNotIn("error", names)
        self.assertEqual(names[-1], "complete")
        result = dict(received)["ROUTE_OPTIMIZE_DONE"]["result"]
        self.assertEqual(result["music"]["artist"], "조용필")

    def test_auth_required_and_stream_path_replaced_without_adding_api(self):
        with TestClient(self.app()) as client:
            self.assertEqual(client.post(PATH, json=http_request()).status_code, 401)
            self.assertEqual(client.post(PATH, json=http_request(), headers={"Authorization": "Bearer wrong"}).status_code, 401)
            self.assertEqual(client.post("/internal/ai/itineraries/generate/stream", json=http_request(), headers=HEADERS).status_code, 404)
            paths = client.get("/openapi.json").json()["paths"]
            self.assertEqual({path for path, methods in paths.items() if "post" in methods}, {
                PATH, "/internal/ai/itineraries/generate", "/internal/ai/music/recommend",
            })
