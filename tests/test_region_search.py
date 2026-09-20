"""Regression for decomposed Hangul in the user's Busan request."""
import unittest
import unicodedata

import httpx
from pydantic import ValidationError

from ai_service.config import Settings
from ai_service.errors import GenerationFailed
from ai_service.places import KakaoPlaces, in_region
from ai_service.schemas import ItineraryStreamRequest, Region


def busan_request():
    return ItineraryStreamRequest.model_validate({
        "generation_job_id": 101,
        "region": {"region_id": 2, "full_name": "부산광역시"},
        "duration": {"arrival_datetime": "2026-10-01T10:00:00", "departure_datetime": "2026-10-03T18:00:00"},
        "headcount": 2, "companion_type": "FRIENDS",
        "preference": {"pace_type": "BALANCED", "transport_type": "PUBLIC_TRANSPORT",
                       "budget_min": 200000, "budget_max": 500000, "budget_type": "KRW",
                       "distance_preference": 50, "themes": ["CULTURE"], "foods": ["CAFE"]},
        "required_places": [],
    })


def place_document(group):
    return {
        "id": group, "place_name": "테스트 장소", "address_name": "부산 해운대구 우동",
        "road_address_name": "부산 해운대구 테스트로 1", "x": "129.16", "y": "35.16",
        "category_group_code": group,
    }


class RegionTests(unittest.TestCase):
    def test_region_normalization_preserves_keys_and_validation(self):
        for value in ("부산광역시", "  부산광역시  "):
            self.assertEqual(Region(region_id=2, full_name=value).model_dump(),
                             {"region_id": 2, "full_name": "부산광역시"})
        for value in (None, 123, "   "):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                Region(region_id=2, full_name=value)

    def test_region_matching_normalizes_both_sides_without_broadening_area(self):
        for region in ("부산광역시 해운대구", unicodedata.normalize("NFD", "부산광역시 해운대구")):
            for address in ("부산 해운대구 우동", unicodedata.normalize("NFD", "부산 해운대구 우동")):
                self.assertTrue(in_region(region, address))
            self.assertFalse(in_region(region, "부산 부산진구 부전동"))
            self.assertFalse(in_region(region, "서울 중구"))


class RegionSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_request_uses_normalized_address_and_candidate_queries(self):
        calls = []

        def handle(req):
            calls.append(req)
            query = req.url.params["query"]
            if query != unicodedata.normalize("NFC", query):
                return httpx.Response(200, json={"documents": []})
            if req.url.path.endswith("/address.json"):
                return httpx.Response(200, json={"documents": [{"x": "129.07", "y": "35.18"}]})
            return httpx.Response(200, json={"documents": [place_document(req.url.params["category_group_code"])]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            pool = await KakaoPlaces(client, Settings(kakao_rest_api_key="test-only")).collect(
                busan_request(), include_accommodation=False
            )
        self.assertEqual(calls[0].url.params["query"], "부산광역시")
        self.assertEqual({p.category for p in pool}, {"관광", "식당"})
        self.assertTrue(all(req.url.params["query"].startswith("부산광역시") for req in calls))
        self.assertTrue(all(req.url.params["query"] == unicodedata.normalize("NFC", req.url.params["query"]) for req in calls))

    async def test_keyword_fallback_uses_same_normalized_region(self):
        calls = []

        def handle(req):
            calls.append(req)
            if req.url.path.endswith("/address.json"):
                documents = []
            elif req.url.params.get("size") == "1":
                documents = [{"x": "129.07", "y": "35.18"}]
            else:
                documents = [place_document(req.url.params["category_group_code"])]
            return httpx.Response(200, json={"documents": documents})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            pool = await KakaoPlaces(client, Settings(kakao_rest_api_key="test-only")).collect(busan_request())
        self.assertTrue(pool)
        self.assertEqual(calls[1].url.params["query"], "부산광역시")
        self.assertTrue(calls[1].url.path.endswith("/keyword.json"))

    async def test_accommodation_region_filter_handles_decomposed_provider_address(self):
        doc = place_document("AD5")
        doc["address_name"] = unicodedata.normalize("NFD", doc["address_name"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"documents": [doc]})
        )) as client:
            result = await KakaoPlaces(client, Settings(kakao_rest_api_key="test-only")).accommodations(
                busan_request(), 35.16, 129.16
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].category, "숙소")

    async def test_genuinely_missing_region_keeps_existing_failure(self):
        calls = []

        def handle(req):
            calls.append(req)
            return httpx.Response(200, json={"documents": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(GenerationFailed) as error:
                await KakaoPlaces(client, Settings(kakao_rest_api_key="test-only")).collect(busan_request())
        self.assertEqual(str(error.exception), "여행 지역의 위치를 찾을 수 없습니다.")
        self.assertEqual(len(calls), 2)
