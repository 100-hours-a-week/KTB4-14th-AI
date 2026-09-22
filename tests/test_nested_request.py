import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from ai_service.api_examples import LEGACY_ITINERARY_REQUEST_EXAMPLE as EXAMPLE
from ai_service.config import Settings
from ai_service.errors import GenerationFailed
from ai_service.main import create_app
from ai_service.schemas import GenerationResult, ItineraryResponse, LegacyGenerationRequest


HEADERS = {"Authorization": "Bearer test-only"}
SETTINGS = Settings(api_token="test-only", openai_api_key="test", kakao_rest_api_key="test")
PATHS = ("/internal/ai/itineraries/generate", "/api/ai/v1/itinerary-jobs/stream")


class NestedRequestTests(unittest.TestCase):
    def test_exact_user_request_reaches_itinerary_generator(self):
        context = LegacyGenerationRequest.model_validate(EXAMPLE).generation_context()
        result = ItineraryResponse(**context.model_dump(), title="제주 일정", days=[])
        generate = AsyncMock(return_value=result)
        with patch("ai_service.main.generate_itinerary", generate), TestClient(create_app(settings=SETTINGS)) as client:
            response = client.post(PATHS[0], json=EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        received = generate.call_args.args[0]
        self.assertEqual(received.generation_job_id, 10)
        self.assertNotIn("travel_plan_id", response.json())
        self.assertEqual(received.region.full_name, EXAMPLE["region"]["full_name"])
        self.assertEqual(received.duration.model_dump(mode="json"), EXAMPLE["duration"])
        self.assertEqual(received.required_places[0].model_dump(), EXAMPLE["required_places"][0])
        self.assertEqual(received.preference.budget_type, "KRW")
        self.assertEqual(received.preference.transport_type, "PUBLIC_TRANSPORT")
        self.assertEqual(response.json()["generation_job_id"], 10)
        self.assertEqual(response.json()["preference"]["budget_type"], "KRW")

    def test_stream_accepts_nested_request_and_preserves_job_identity(self):
        async def pipeline(body, *args):
            self.assertEqual(body.generation_job_id, 10)
            yield "PLACES", "STARTED", None
            yield "COMPLETE", "COMPLETED", GenerationResult(
                itinerary=ItineraryResponse(**body.model_dump(), title="제주 일정", days=[]),
                music={"title": "테스트", "artist": "테스트", "youtube_url": "https://www.youtube.com/results?search_query=test"},
            )

        with patch("ai_service.streaming.generation_stages", side_effect=pipeline), TestClient(create_app(settings=SETTINGS)) as client:
            response = client.post(PATHS[1], json=EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-2]["generation_job_id"], 10)
        self.assertNotIn("travel_plan_id", events[-2])
        self.assertEqual(events[-2]["result"]["title"], "제주 일정")
        self.assertEqual(sum("result" in event for event in events), 1)

    def test_failure_reports_job_id_without_treating_it_as_travel_plan_id(self):
        with patch("ai_service.main.generate_itinerary", AsyncMock(side_effect=GenerationFailed("테스트 실패"))), TestClient(create_app(settings=SETTINGS)) as client:
            response = client.post(PATHS[0], json=EXAMPLE, headers=HEADERS)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["data"]["generation_job_id"], 10)
        self.assertNotIn("travel_plan_id", response.json()["data"])

    def test_conflicting_shapes_and_duplicate_places_not_silently_accepted(self):
        changes = [
            {"travel_plan_id": 123},
            {"region_name": "다른 지역"},
            {"preference": {**EXAMPLE["preference"], "budget_currency": "USD"}},
            {"required_places": EXAMPLE["required_places"] * 2},
        ]
        with TestClient(create_app(settings=SETTINGS)) as client:
            for path in PATHS:
                for change in changes:
                    with self.subTest(path=path, change=change):
                        response = client.post(path, json={**copy.deepcopy(EXAMPLE), **change}, headers=HEADERS)
                        self.assertEqual(response.status_code, 400)

    def test_swagger_exposes_only_confirmed_request_example(self):
        spec = create_app(settings=SETTINGS).openapi()
        properties = spec["components"]["schemas"]["LegacyGenerationPreference"]["properties"]
        self.assertIn("budget_type", properties)
        self.assertNotIn("budget_currency", properties)
        for path in PATHS:
            content = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]
            self.assertEqual(list(content["examples"]), ["nested"])
            self.assertEqual(content["examples"]["nested"]["value"], EXAMPLE)
            self.assertEqual({ref["$ref"].split("/")[-1] for ref in content["schema"]["anyOf"]},
                             {"LegacyGenerationRequest", "TravelGenerationRequest"})

    def test_400_identifies_request_field_without_echoing_values_or_other_schema(self):
        body = {**EXAMPLE, "unexpected": "private-value-not-for-response"}
        del body["generation_job_id"]
        with TestClient(create_app(settings=SETTINGS)) as client:
            for path in PATHS:
                response = client.post(path, json=body, headers=HEADERS)
                self.assertEqual(response.status_code, 400)
                detail = response.json()["data"]["error_message"]
                self.assertIn("generation_job_id", detail)
                self.assertIn("unexpected", detail)
                self.assertNotIn("travel_plan_id", detail)
                self.assertNotIn("private-value-not-for-response", response.text)
            response = client.post(PATHS[0], content='{"generation_job_id": 10,\\}',
                                   headers={**HEADERS, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("JSON 문법", response.json()["data"]["error_message"])
