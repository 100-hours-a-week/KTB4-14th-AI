"""야간 관광지의 오전 배치 방지·순서 보정·실제 경로 후 검증 회귀 검사."""
from datetime import datetime
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from ai_service.core.config import Settings
from ai_service.core.errors import GenerationFailed, InvalidModelOutput
from ai_service.main import create_app
from ai_service.schemas.common import KST
from ai_service.schemas.music import MusicRecommendation
from ai_service.schemas.travel import ItineraryRequest, ModelSelection, Place, RouteDetails
from ai_service.services.travel.opening_hours import OpeningHours
from ai_service.services.travel.place_hours import fit_visit, hours_for
from ai_service.services.travel.pipeline import connect_routes, recommend_places, replace_closed_restaurant
from ai_service.services.travel.scheduling import build_context, schedule_selection, validate_itinerary


def case():
    body = ItineraryRequest.model_validate({
        "generation_job_id": 41,
        "region": {"region_id": 1, "full_name": "제주특별자치도 서귀포시 안덕면"},
        "duration": {"arrival_datetime": "2026-10-29T10:00:00", "departure_datetime": "2026-10-29T23:59:00"},
        "headcount": 2, "companion_type": "COUPLE",
        "preference": {"pace_type": "BALANCED", "transport_type": "CAR", "themes": ["NATURE"]},
        "required_places": [{"provider": "KAKAO", "provider_place_id": "luna", "place_name": "루나폴",
                             "address": "제주특별자치도 서귀포시 안덕면 일주서로 1836",
                             "latitude": 33.257455, "longitude": 126.322523, "category": "관광", "order": 1}],
    })
    pool = [Place(provider_place_id=pid, place_name=name,
                  address="제주특별자치도 서귀포시 안덕면 일주서로 1836",
                  latitude=33.257455 + i * .001, longitude=126.322523 + i * .001,
                  category=category, source_category=category, is_required=(pid == "luna"))
            for i, (pid, name, category) in enumerate([
                ("luna", "루나폴", "관광"), ("food1", "테스트 점심", "식당"),
                ("tour", "테스트 주간 관광지", "관광"), ("food2", "테스트 저녁", "식당"),
                ("tour2", "다른 관광지", "관광"),
            ])]
    selected = ModelSelection(title="야간 관광지 테스트", days=[{
        "date": "2026-10-29", "items": [{"provider_place_id": pid} for pid in ("luna", "food1", "tour", "food2")],
    }])
    return body, pool, selected


class PlaceHoursTests(unittest.IsolatedAsyncioTestCase):
    def test_lunafall_name_and_address_must_match_and_hours_are_private(self):
        _, pool, _ = case()
        luna = pool[0]
        self.assertIsNotNone(hours_for(luna))
        self.assertNotIn("opening_hours", luna.model_dump())
        self.assertNotIn("_opening_hours", luna.model_dump())
        other = luna.model_copy(deep=True)
        other._opening_hours = None
        other.address = "서울특별시 종로구 일주서로 1836"
        self.assertIsNone(hours_for(other))
        other.address = luna.address
        other.place_name = "루나폴 카페"
        self.assertIsNone(hours_for(other))

    def test_road_address_and_abbreviated_jeju_and_unicode_are_supported(self):
        import unicodedata
        _, pool, _ = case()
        luna = pool[0]
        luna.place_name = unicodedata.normalize("NFD", "루나폴")
        luna.address = "제주 서귀포시 안덕면 덕수리 산 27"
        self.assertIsNotNone(hours_for(luna))
        luna._opening_hours = None
        luna.address = "제주 서귀포시 안덕면 다른 주소"
        luna.road_address = "제주 서귀포시 안덕면 일주서로 1836"
        self.assertIsNotNone(hours_for(luna))

    def test_lunafall_cannot_start_before_19_and_must_finish_before_daily_end(self):
        _, pool, _ = case()
        for day in (29, 30, 31):
            for tz in (None, KST):
                morning = datetime(2026, 10, day, 11, tzinfo=tz)
                end = datetime(2026, 10, day, 23, 59, tzinfo=tz)
                self.assertEqual(fit_visit(pool[0], morning, 60, end).hour, 19)
        with self.assertRaisesRegex(ValueError, "place_closed:luna"):
            fit_visit(pool[0], datetime(2026, 10, 29, 23, 30), 60, datetime(2026, 10, 29, 23, 59))

    def test_overnight_break_and_holiday_are_shared_with_restaurant_engine(self):
        h = OpeningHours({"weekly": {d: [["19:00", "00:00"]] for d in
                         ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
                         "exceptions": {"2026-10-30": []}})
        self.assertTrue(h.contains(datetime(2026, 10, 29, 23), datetime(2026, 10, 30)))
        self.assertFalse(h.contains(datetime(2026, 10, 30, 19), datetime(2026, 10, 30, 20)))

    def test_internal_model_context_includes_known_hours_but_does_not_invent_unknown_hours(self):
        body, pool, _ = case()
        context = build_context(body, pool)
        luna = next(p for p in context["candidates"] if p["provider_place_id"] == "luna")
        self.assertEqual(luna["opening_hours"]["weekly"]["thu"], [["19:00", "24:00"]])
        unknown = next(p for p in context["candidates"] if p["provider_place_id"] == "tour")
        self.assertNotIn("opening_hours", unknown)

    async def test_even_if_model_selects_lunafall_first_server_moves_it_after_day_visits(self):
        body, pool, selected = case()
        planner = type("Planner", (), {"generate": AsyncMock(return_value=selected)})()
        repaired = await recommend_places(body, pool, planner)
        ids = [item.provider_place_id for item in repaired.days[0].items]
        self.assertNotEqual(ids[0], "luna")
        self.assertIn("luna", ids)
        generated = schedule_selection(body, repaired, pool)
        validate_itinerary(body, generated, pool)
        self.assertGreaterEqual(next(i.start_time for i in generated.days[0].items if i.provider_place_id == "luna"), "19:00")
        self.assertEqual(selected.days[0].items[0].provider_place_id, "luna")

    def test_final_validation_rejects_morning_visit_even_when_full_itinerary_is_otherwise_valid(self):
        body, pool, selected = case()
        repaired = replace_closed_restaurant(body, selected, pool)
        generated = schedule_selection(body, repaired, pool)
        next(i for i in generated.days[0].items if i.provider_place_id == "luna").start_time = "11:00"
        with self.assertRaisesRegex(InvalidModelOutput, "place_closed:luna"):
            validate_itinerary(body, generated, pool)

    async def test_real_route_reschedules_and_json_contract_does_not_expose_hours(self):
        body, pool, selected = case()
        router = type("Router", (), {"route": AsyncMock(return_value=RouteDetails(
            transport_type="CAR", duration_minutes=18, distance_meter=3700))})()
        result = await connect_routes(body, selected, pool, "test", router)
        luna = next(i for i in result.itinerary.days[0].items if i.provider_place_id == "luna")
        self.assertGreaterEqual(luna.start_time, "19:00")
        payload = json.dumps(result.itinerary.model_dump(mode="json"))
        for field in ("opening_hours", "weekly", "source_note", "_opening_hours"):
            self.assertNotIn(field, payload)

    async def test_closed_optional_tour_is_replaced_and_required_tour_is_not_silently_removed(self):
        body, pool, selected = case()
        body.required_places = []
        pool[0].is_required = False
        selected.days[0].items = selected.days[0].items[1:] + selected.days[0].items[:1]
        pool[2]._opening_hours = OpeningHours({"weekly": {d: [] for d in
                                              ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}})
        router = type("Router", (), {"route": AsyncMock(return_value=RouteDetails(
            transport_type="CAR", duration_minutes=5, distance_meter=1000))})()
        result = await connect_routes(body, selected, pool, "test", router)
        self.assertNotIn("tour", [i.provider_place_id for i in result.itinerary.days[0].items])
        pool[2].is_required = True
        with self.assertRaises(GenerationFailed):
            await connect_routes(body, selected, pool, "test", router)

    def test_http_generate_uses_same_fix_without_new_response_fields(self):
        body, pool, selected = case()
        app = create_app(settings=Settings(api_token="test-only", openai_api_key="test-only", kakao_rest_api_key="test-only"),
                         transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError("External HTTP forbidden"))))
        with TestClient(app) as client:
            app.state.places.collect = AsyncMock(return_value=pool)
            app.state.planner.generate = AsyncMock(return_value=selected)
            app.state.planner.recommend_music = AsyncMock(return_value=MusicRecommendation(
                title="테스트 곡", artist="테스트", youtube_url="https://www.youtube.com/watch?v=abcdefghijk"))
            response = client.post("/internal/ai/itineraries/generate", json=body.model_dump(mode="json", exclude_none=True),
                                   headers={"Authorization": "Bearer test-only"})
        self.assertEqual(response.status_code, 200, response.text)
        luna = next(i for i in response.json()["days"][0]["items"] if i["provider_place_id"] == "luna")
        self.assertGreaterEqual(luna["start_time"], "19:00")
        self.assertNotIn("opening_hours", response.text)
