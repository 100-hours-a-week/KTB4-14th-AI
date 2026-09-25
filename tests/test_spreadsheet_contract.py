import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_service.api_examples import ITINERARY_REQUEST_EXAMPLE, MUSIC_REQUEST_EXAMPLE, LEGACY_ITINERARY_REQUEST_EXAMPLE
from ai_service.config import Settings
from ai_service.main import create_app
from ai_service.schemas import GenerationResult, ItineraryRequest, ItineraryResponse, ItineraryStreamRequest, MusicRequest, TravelGenerationRequest, LegacyGenerationRequest


HEADERS = {"Authorization": "Bearer test-only"}


class SpreadsheetContractTests(unittest.TestCase):
    def test_exact_trip_request_reaches_generator_without_adding_response_fields(self):
        settings = Settings(api_token="test-only", openai_api_key="test", kakao_rest_api_key="test")
        body = TravelGenerationRequest.model_validate(ITINERARY_REQUEST_EXAMPLE).generation_context()
        result = ItineraryResponse(**body.model_dump(), title="테스트 일정", days=[])
        generate = AsyncMock(return_value=result)
        with patch("ai_service.main.generate_itinerary", generate), TestClient(create_app(settings=settings)) as client:
            response = client.post("/internal/ai/itineraries/generate", json=ITINERARY_REQUEST_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        received = generate.call_args.args[0]
        self.assertNotIn("client_draft_id", received.model_dump())
        self.assertEqual(received.preference.budget_type, "KRW")
        self.assertEqual(received.required_places[0].provider_place_id, "26338954")
        self.assertNotIn("client_draft_id", response.json())
        self.assertEqual(response.json(), result.model_dump(mode="json"))
        self.assertNotIn("generation_job_id", response.json())
        self.assertEqual(received.region.full_name, "제주특별자치도 서귀포시")
        self.assertNotIn("budget_currency", response.json()["preference"])

    def test_exact_trip_request_also_accepted_by_stream(self):
        settings = Settings(api_token="test-only", openai_api_key="test", kakao_rest_api_key="test")

        async def pipeline(body, *args):
            self.assertNotIn("client_draft_id", body.model_dump())
            itinerary = ItineraryResponse(**body.itinerary_request().model_dump(), title="테스트 일정", days=[])
            yield "PLACES", "STARTED", None
            yield "COMPLETE", "COMPLETED", GenerationResult(itinerary=itinerary, music={
                "title": "테스트", "artist": "테스트", "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
            })

        with patch("ai_service.streaming.generation_stages", side_effect=pipeline), TestClient(create_app(settings=settings)) as client:
            response = client.post("/api/ai/v1/itinerary-jobs/stream", json=ITINERARY_REQUEST_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: complete\n", response.text)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-2]["result"]["title"], "테스트 일정")
        self.assertEqual(events[-2]["travel_plan_id"], ITINERARY_REQUEST_EXAMPLE["travel_plan_id"])
        self.assertEqual(events[-1]["status"], "COMPLETED")
        self.assertNotIn("data", events[-1])
        self.assertNotIn("budget_currency", response.text)
        self.assertNotIn("client_draft_id", response.text)

    def test_removed_draft_field_rejected_and_absent_from_openapi(self):
        body = {**ITINERARY_REQUEST_EXAMPLE, "client_draft_id": 1}
        with TestClient(create_app(settings=Settings(api_token="test-only"))) as client:
            for endpoint in ("/internal/ai/itineraries/generate", "/api/ai/v1/itinerary-jobs/stream"):
                with self.subTest(endpoint=endpoint):
                    response = client.post(endpoint, json=body, headers=HEADERS)
                    self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn("client_draft_id", client.get("/openapi.json").text)

    def test_both_currency_names_are_not_silently_combined(self):
        data = copy.deepcopy(ITINERARY_REQUEST_EXAMPLE)
        data["preference"]["budget_currency"] = "USD"
        with self.assertRaises(ValidationError):
            TravelGenerationRequest.model_validate(data)

    def test_music_request_recommends_song_and_returns_verified_video(self):
        calls = []
        def handle(req):
            if req.method == "POST":
                payload = json.loads(req.content)
                calls.append(payload)
                context = json.loads(payload["messages"][1]["content"])
                self.assertEqual(context, {k: MUSIC_REQUEST_EXAMPLE[k] for k in ("region", "duration", "preference")})
                suggestion = {"title": "Unverified" if len(calls) == 1 else "Yellow", "artist": "Coldplay"}
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(suggestion)}}]})
            self.assertEqual(req.url.host, "www.youtube.com")
            return httpx.Response(200, json={"type": "video", "title": "Coldplay - Yellow", "author_name": "Coldplay"})
        search = AsyncMock(side_effect=[[], [{"id": "abcdefghijk", "title": "Coldplay - Yellow", "channel": "Coldplay"}]])
        settings = Settings(api_token="test-only", openai_api_key="test")
        with patch("ai_service.music.YouTubeMusic._search", search), TestClient(create_app(settings=settings, transport=httpx.MockTransport(handle))) as client:
            response = client.post("/internal/ai/music/recommend", json=MUSIC_REQUEST_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "message": "ai_music_recommended",
            "data": {"travel_plan_id": 1, "title": "Yellow", "artist": "Coldplay", "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk"},
        })
        self.assertEqual(len(calls), 2)

    def test_failed_music_recommendation_returns_422_with_travel_plan_id(self):
        def handle(req):
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"title":"Unknown","artist":"Unknown"}'}}]})
        with patch("ai_service.music.YouTubeMusic._search", AsyncMock(return_value=[])), TestClient(create_app(settings=Settings(api_token="test-only", openai_api_key="test"), transport=httpx.MockTransport(handle))) as client:
            response = client.post("/internal/ai/music/recommend", json=MUSIC_REQUEST_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["message"], "ai_music_recommendation_failed")
        self.assertEqual(response.json()["data"]["travel_plan_id"], 1)
        self.assertNotIn("generation_job_id", response.json()["data"])

    def test_music_requires_model_configuration(self):
        with TestClient(create_app(settings=Settings(api_token="test-only"))) as client:
            response = client.post("/internal/ai/music/recommend", json=MUSIC_REQUEST_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 503)

    def test_music_requires_auth_and_rejects_candidate_or_song_input(self):
        with TestClient(create_app(settings=Settings(api_token="test-only"))) as client:
            self.assertEqual(client.post("/internal/ai/music/recommend", json=MUSIC_REQUEST_EXAMPLE).status_code, 401)
            for extra in ({"candidates": []}, {"title": "개화"}, {"artist": "LUCY"}, {"youtube_url": "https://www.youtube.com/watch?v=abcdefghijk"}):
                body = {**MUSIC_REQUEST_EXAMPLE, **extra}
                self.assertEqual(client.post("/internal/ai/music/recommend", json=body, headers=HEADERS).status_code, 400)
            schema = client.get("/openapi.json").json()["components"]["schemas"]
            self.assertEqual(set(schema["MusicRequest"]["properties"]), {"travel_plan_id", "region", "duration", "preference"})
            self.assertNotIn("music_id", schema["SelectedMusic"]["properties"])

    def test_openapi_examples_match_spreadsheet_requests(self):
        spec = create_app(settings=Settings()).openapi()
        for endpoint, example, schema, example_key in (
            ("/internal/ai/itineraries/generate", LEGACY_ITINERARY_REQUEST_EXAMPLE, LegacyGenerationRequest, "nested"),
            ("/api/ai/v1/itinerary-jobs/stream", LEGACY_ITINERARY_REQUEST_EXAMPLE, LegacyGenerationRequest, "nested"),
            ("/internal/ai/music/recommend", MUSIC_REQUEST_EXAMPLE, MusicRequest, "travel"),
        ):
            value = spec["paths"][endpoint]["post"]["requestBody"]["content"]["application/json"]["examples"][example_key]["value"]
            self.assertEqual(value, example)
            schema.model_validate(value)
