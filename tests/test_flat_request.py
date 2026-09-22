import copy
import unicodedata
import unittest

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_service.api_examples import ITINERARY_REQUEST_EXAMPLE
from ai_service.config import Settings
from ai_service.errors import GenerationFailed
from ai_service.main import create_app
from ai_service.places import KakaoPlaces
from ai_service.schemas import TravelGenerationRequest
from test_day_transport import request


class FlatRequestTests(unittest.TestCase):
    def test_table_fields_and_required_optional_rules(self):
        spec = TravelGenerationRequest.model_json_schema()
        self.assertEqual(set(spec["properties"]), {
            "travel_plan_id", "region_id", "region_name", "arrival_datetime", "departure_datetime", "headcount",
            "companion_type", "preference", "required_places",
        })
        self.assertEqual(set(spec["required"]), set(spec["properties"]) - {"required_places"})
        body = copy.deepcopy(ITINERARY_REQUEST_EXAMPLE)
        place = body["required_places"][0]
        for key in ("address",):
            place.pop(key)
        for key in ("budget_min", "budget_max", "foods", "extra_request"):
            body["preference"].pop(key)
        parsed = TravelGenerationRequest.model_validate(body)
        context = parsed.generation_context()
        self.assertIsNone(context.generation_job_id)
        self.assertEqual(context.duration.model_dump(mode="json")["arrival_datetime"], body["arrival_datetime"])
        for key in ("budget_type", "distance_preference", "pace_type", "transport_type", "themes"):
            invalid = copy.deepcopy(body)
            invalid["preference"].pop(key)
            with self.subTest(key=key), self.assertRaises(ValidationError):
                TravelGenerationRequest.model_validate(invalid)
        for key in ("provider", "provider_place_id"):
            invalid = copy.deepcopy(body)
            invalid["required_places"][0].pop(key)
            with self.subTest(key=key), self.assertRaises(ValidationError):
                TravelGenerationRequest.model_validate(invalid)

    def test_flat_dates_and_duplicate_required_places_still_validated(self):
        for change in (
            {"departure_datetime": "2026-08-26T18:00:00"},
            {"arrival_datetime": "2026-08-27"},
            {"required_places": ITINERARY_REQUEST_EXAMPLE["required_places"] * 2},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                TravelGenerationRequest.model_validate({**ITINERARY_REQUEST_EXAMPLE, **change})

    def test_backend_region_name_used_without_registry(self):
        body = {**ITINERARY_REQUEST_EXAMPLE, "region_id": 98765,
                "region_name": unicodedata.normalize("NFD", "부산광역시")}
        context = TravelGenerationRequest.model_validate(body).generation_context()
        self.assertEqual(context.region.region_id, 98765)
        self.assertEqual(context.region.full_name, "부산광역시")
        self.assertEqual(context.travel_plan_id, body["travel_plan_id"])

    def test_empty_themes_and_null_order_accepted(self):
        body = copy.deepcopy(ITINERARY_REQUEST_EXAMPLE)
        body["preference"]["themes"] = []
        body["required_places"][0]["order"] = None
        context = TravelGenerationRequest.model_validate(body).generation_context()
        self.assertEqual(context.preference.themes, [])
        self.assertEqual(context.required_places[0].order, 1)
        for kind, category in (("TOURISM", "관광"), ("RESTAURANT", "식당"), ("ACCOMMODATION", "숙소")):
            body["required_places"][0]["place_type"] = kind
            self.assertEqual(TravelGenerationRequest.model_validate(body).generation_context().required_places[0].category, category)

    def test_regeneration_null_metadata_returns_explanation_before_external_calls(self):
        body = copy.deepcopy(ITINERARY_REQUEST_EXAMPLE)
        body["required_places"][0].update(place_name=None, address=None, latitude=None, longitude=None)
        with TestClient(create_app(settings=Settings(api_token="test-only"))) as client:
            for endpoint in ("/internal/ai/itineraries/generate", "/api/ai/v1/itinerary-jobs/stream"):
                response = client.post(endpoint, json=body, headers={"Authorization": "Bearer test-only"})
                self.assertEqual(response.status_code, 422)
                self.assertIn("재생성 장소 상세", response.json()["data"]["error_message"])
                self.assertEqual(response.json()["data"]["travel_plan_id"], body["travel_plan_id"])


class OptionalPlaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_address_and_category_resolved_by_exact_provider_id(self):
        from test_region_search import place_document

        body = request()
        body.required_places = TravelGenerationRequest.model_validate({
            **ITINERARY_REQUEST_EXAMPLE,
            "required_places": [{"provider": "KAKAO", "provider_place_id": "required",
                                 "place_name": "테스트 장소", "latitude": 35.16,
                                 "longitude": 129.16, "order": 1}],
        }).generation_context().required_places
        body.required_places[0].category = ""

        def handle(req):
            if req.url.params.get("radius") == "2000":
                return httpx.Response(200, json={"documents": [
                    {**place_document("FD6"), "id": "other", "address_name": "잘못된 주소"},
                    {**place_document("AT4"), "id": "required"},
                ]})
            if req.url.path.endswith("/address.json"):
                return httpx.Response(200, json={"documents": [{"x": "129.16", "y": "35.16"}]})
            return httpx.Response(200, json={"documents": [place_document(req.url.params.get("category_group_code") or "AT4")]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            pool = await KakaoPlaces(client, Settings(kakao_rest_api_key="test")).collect(body)
        self.assertEqual(body.required_places[0].category, "AT4")
        required = next(p for p in pool if p.provider_place_id == "required")
        self.assertEqual(required.category, "관광")
        self.assertEqual(required.address, "부산 해운대구 우동")

    async def test_missing_place_metadata_not_taken_from_different_id(self):
        body = TravelGenerationRequest.model_validate(ITINERARY_REQUEST_EXAMPLE).generation_context()
        body.required_places[0].category = ""
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"documents": [{"id": "wrong", "category_group_code": "AT4"}]}))
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaises(GenerationFailed):
                await KakaoPlaces(client, Settings(kakao_rest_api_key="test")).collect(body)
