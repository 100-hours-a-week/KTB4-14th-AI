"""Regression for decomposed Hangul in the user's Busan request."""
import unittest
import unicodedata

import httpx
from pydantic import ValidationError

from ai_service.config import Settings
from ai_service.errors import GenerationFailed, ServiceUnavailable
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
    async def test_provider_rejection_is_not_reported_as_no_places(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(429, json={"message": "private-provider-body"})
        )) as client:
            with self.assertLogs("uvicorn.error.audigo", level="INFO") as logs, self.assertRaises(ServiceUnavailable):
                await KakaoPlaces(client, Settings(kakao_rest_api_key="secret-key")).collect(busan_request())
        log = "".join(logs.output)
        self.assertIn('"http_status": 429', log)
        self.assertNotIn("secret-key", log)
        self.assertNotIn("private-provider-body", log)

    async def test_provider_canonical_region_accepts_alias_and_still_rejects_other_city(self):
        request = busan_request()
        request.region.full_name = "강릉"

        def handle(req):
            if req.url.path.endswith("/address.json"):
                docs = [{"address_type": "REGION", "address_name": "강원특별자치도 강릉시", "x": "128.87", "y": "37.75"}]
            else:
                group = req.url.params.get("category_group_code", "AT4")
                inside = {**place_document(group), "address_name": "강원 강릉시 교동"}
                outside = {**inside, "id": "outside", "address_name": "강원 속초시 교동"}
                docs = [inside, outside]
            return httpx.Response(200, json={"documents": docs})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            places = KakaoPlaces(client, Settings(kakao_rest_api_key="test-only"))
            pool = await places.collect(request, include_accommodation=False)
            hotels = await places.accommodations(request, 37.75, 128.87)
        self.assertEqual({p.category for p in pool}, {"관광", "식당"})
        self.assertEqual(len(hotels), 1)
        self.assertTrue(all(p.provider_place_id != "outside" for p in pool + hotels))
        self.assertEqual(request.region.full_name, "강릉")

    async def test_empty_keywords_fall_back_once_to_categories_with_same_radius(self):
        calls = []

        def handle(req):
            calls.append(req)
            if req.url.path.endswith("/address.json"):
                docs = [{"x": "129.07", "y": "35.18"}]
            elif req.url.path.endswith("/category.json"):
                docs = [place_document(req.url.params["category_group_code"])]
            else:
                docs = []
            return httpx.Response(200, json={"documents": docs})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            pool = await KakaoPlaces(client, Settings(kakao_rest_api_key="test-only")).collect(busan_request(), include_accommodation=False)
        self.assertEqual({p.category for p in pool}, {"관광", "식당"})
        fallback = [r for r in calls if r.url.path.endswith("/category.json")]
        self.assertEqual(len(fallback), 2)
        self.assertEqual({r.url.params["radius"] for r in calls if "radius" in r.url.params}, {"8000"})

    async def test_empty_pool_has_diagnostics_and_preserves_error_contract(self):
        def handle(req):
            docs = [{"x": "129.07", "y": "35.18"}] if req.url.path.endswith("/address.json") else [
                {**place_document("AT4"), "address_name": "서울 중구"}]
            return httpx.Response(200, json={"documents": docs})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertLogs("uvicorn.error.audigo", level="INFO") as logs, self.assertRaises(GenerationFailed) as error:
                await KakaoPlaces(client, Settings(kakao_rest_api_key="secret-test-value")).collect(busan_request())
        self.assertEqual(error.exception.code, "ai_itinerary_generation_failed")
        self.assertEqual(error.exception.reason, "no_place_candidates")
        self.assertIn('"outside_region":', logs.output[0])
        self.assertNotIn("secret-test-value", "".join(logs.output))

    async def test_ambiguous_region_does_not_choose_first_city(self):
        docs = [{"address_type": "REGION", "address_name": name, "x": "129", "y": "35"}
                for name in ["부산 중구", "서울 중구"]]
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"documents": docs}))) as client:
            with self.assertRaises(GenerationFailed) as error:
                await KakaoPlaces(client, Settings(kakao_rest_api_key="test-only")).collect(busan_request())
        self.assertEqual(error.exception.reason, "ambiguous_region")

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

    async def test_accommodations_fall_back_across_border_only_when_region_has_none(self):
        doc = {**place_document("AD5"), "id": "across", "address_name": "경남 양산시 물금읍"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"documents": [doc]})
        )) as client:
            with self.assertLogs("uvicorn.error.audigo", level="INFO") as logs:
                result = await KakaoPlaces(client, Settings(kakao_rest_api_key="test-only")).accommodations(
                    busan_request(), 35.3, 129.0
                )
        self.assertEqual([p.provider_place_id for p in result], ["across"])
        self.assertIn("accommodation_region_fallback", "\n".join(logs.output))

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
