import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_service.api_examples import ITINERARY_REQUEST_EXAMPLE, MUSIC_REQUEST_EXAMPLE
from ai_service.config import Settings
from ai_service.main import create_app
from ai_service.schemas import GenerationResult, ItineraryRequest, ItineraryResponse, ItineraryStreamRequest, MusicRequest


HEADERS = {"Authorization": "Bearer test-only"}


class SpreadsheetContractTests(unittest.TestCase):
    def test_exact_trip_request_reaches_generator_without_adding_response_fields(self):
        settings = Settings(api_token="test-only", openai_api_key="test", kakao_rest_api_key="test")
        body = ItineraryRequest.model_validate(ITINERARY_REQUEST_EXAMPLE)
        result = ItineraryResponse(**body.model_dump(), title="테스트 일정", days=[])
        generate = AsyncMock(return_value=result)
        with patch("ai_service.main.generate_itinerary", generate), TestClient(create_app(settings=settings)) as client:
            response = client.post("/internal/ai/itineraries/generate", json=ITINERARY_REQUEST_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        received = generate.call_args.args[0]
        self.assertEqual(received.client_draft_id, 1)
        self.assertEqual(received.preference.budget_currency, "KRW")
        self.assertEqual(received.required_places[0].provider_place_id, "26338954")
        self.assertEqual(response.json()["client_draft_id"], 1)
        for key, value in ITINERARY_REQUEST_EXAMPLE.items():
            self.assertEqual(response.json()[key], value)
        self.assertNotIn("budget_type", response.json()["preference"])

    def test_exact_trip_request_also_accepted_by_stream(self):
        settings = Settings(api_token="test-only", openai_api_key="test", kakao_rest_api_key="test")

        async def pipeline(body, *args):
            self.assertEqual(body.client_draft_id, 1)
            itinerary = ItineraryResponse(**body.itinerary_request().model_dump(), title="테스트 일정", days=[])
            yield "PLACES", "STARTED", None
            yield "COMPLETE", "COMPLETED", GenerationResult(itinerary=itinerary, music={
                "title": "테스트", "artist": "테스트", "youtube_url": "https://www.youtube.com/results?search_query=test",
            })

        with patch("ai_service.streaming.generation_stages", side_effect=pipeline), TestClient(create_app(settings=settings)) as client:
            response = client.post("/internal/ai/itineraries/generate/stream", json=ITINERARY_REQUEST_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: complete\n", response.text)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        itinerary = events[-1]["data"]["itinerary"]
        for key, value in ITINERARY_REQUEST_EXAMPLE.items():
            self.assertEqual(itinerary[key], value)
        self.assertNotIn("budget_type", response.text)

    def test_both_currency_names_are_not_silently_combined(self):
        data = copy.deepcopy(ITINERARY_REQUEST_EXAMPLE)
        data["preference"]["budget_type"] = "USD"
        with self.assertRaises(ValidationError):
            ItineraryRequest.model_validate(data)

    def test_exact_music_request_selects_only_candidate_and_preserves_metadata(self):
        # A single supplied candidate needs no model or catalog API call.
        with TestClient(create_app(settings=Settings(api_token="test-only"))) as client:
            response = client.post("/internal/ai/music/recommend", json=MUSIC_REQUEST_EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "message": "ai_music_recommended",
            "data": {"travel_plan_id": 1, **MUSIC_REQUEST_EXAMPLE["candidates"][0]},
        })

    def test_multiple_candidates_retry_invalid_id_and_keep_original_metadata(self):
        calls = []

        def handle(req):
            self.assertEqual(req.url.host, "api.openai.com")
            payload = json.loads(req.content)
            calls.append(payload)
            self.assertEqual(payload["response_format"]["json_schema"]["schema"]["properties"]["music_id"]["enum"], [3, 4])
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"music_id": 999 if len(calls) == 1 else 4}),
            }}]})

        data = copy.deepcopy(MUSIC_REQUEST_EXAMPLE)
        other = {"music_id": 4, "title": "다른 곡", "artist": "테스트", "youtube_url": "https://www.youtube.com/watch?v=test"}
        data["candidates"].append(other)
        settings = Settings(api_token="test-only", openai_api_key="test")
        with TestClient(create_app(settings=settings, transport=httpx.MockTransport(handle))) as client:
            response = client.post("/internal/ai/music/recommend", json=data, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], {"travel_plan_id": 1, **other})
        self.assertEqual(len(calls), 2)

    def test_failed_music_selection_returns_422_with_travel_plan_id(self):
        def handle(req):
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"music_id":999}'}}]})

        data = copy.deepcopy(MUSIC_REQUEST_EXAMPLE)
        data["candidates"].append({**data["candidates"][0], "music_id": 4})
        with TestClient(create_app(settings=Settings(api_token="test-only", openai_api_key="test"), transport=httpx.MockTransport(handle))) as client:
            response = client.post("/internal/ai/music/recommend", json=data, headers=HEADERS)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["message"], "ai_music_recommendation_failed")
        self.assertEqual(response.json()["data"]["travel_plan_id"], 1)
        self.assertNotIn("generation_job_id", response.json()["data"])

    def test_music_requires_auth_and_valid_unique_candidates(self):
        with TestClient(create_app(settings=Settings(api_token="test-only"))) as client:
            self.assertEqual(client.post("/internal/ai/music/recommend", json=MUSIC_REQUEST_EXAMPLE).status_code, 401)
            for candidates in ([], MUSIC_REQUEST_EXAMPLE["candidates"] * 2):
                body = {**MUSIC_REQUEST_EXAMPLE, "candidates": candidates}
                self.assertEqual(client.post("/internal/ai/music/recommend", json=body, headers=HEADERS).status_code, 400)

    def test_openapi_examples_match_spreadsheet_requests(self):
        spec = create_app(settings=Settings()).openapi()
        for endpoint, example, schema in (
            ("/internal/ai/itineraries/generate", ITINERARY_REQUEST_EXAMPLE, ItineraryRequest),
            ("/internal/ai/itineraries/generate/stream", ITINERARY_REQUEST_EXAMPLE, ItineraryStreamRequest),
            ("/internal/ai/music/recommend", MUSIC_REQUEST_EXAMPLE, MusicRequest),
        ):
            value = spec["paths"][endpoint]["post"]["requestBody"]["content"]["application/json"]["examples"]["spreadsheet"]["value"]
            self.assertEqual(value, example)
            schema.model_validate(value)
