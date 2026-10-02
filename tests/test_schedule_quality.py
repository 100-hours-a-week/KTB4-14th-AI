from datetime import datetime
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from ai_service.config import Settings
from ai_service.errors import GenerationFailed, InvalidModelOutput
from ai_service.features import complete_selection, day_windows, minimum_day_minutes, schedule_selection, validate_generation_window, validate_itinerary
from ai_service.pipeline import fallback_multi_day, recommend_accommodations, recommend_places, trim_places_to_time, validate_places_selection
from ai_service.routing import KakaoRoutes, schedule_with_routes
from ai_service.schemas import KST, ModelSelection, RequiredPlace
from test_day_transport import places, request, selection


class ScheduleQualityTests(unittest.IsolatedAsyncioTestCase):
    def scenario(self):
        body = request(None, "CAR")
        body.duration.arrival_datetime = body.duration.arrival_datetime.replace(hour=13)
        pool = places()
        pool.append(pool[1].model_copy(update={"provider_place_id": "6", "place_name": "두번째 식당"}))
        selected = selection()
        selected.days[0].items.insert(2, selected.days[0].items[1].model_copy(update={"provider_place_id": "6"}))
        return body, pool, selected

    async def test_optional_consecutive_meal_removed_and_both_schedulers_end_day_at_hotel(self):
        body, pool, selected = self.scenario()
        completed = complete_selection(body, selected, pool)
        ids = [item.provider_place_id for item in completed.days[0].items]
        self.assertEqual(ids, ["0", "1", "2"])
        estimated = validate_itinerary(body, schedule_selection(body, completed, pool), pool)
        async with httpx.AsyncClient() as client:
            routed = (await schedule_with_routes(body, completed, pool, "test", KakaoRoutes(client, Settings()))).itinerary.days
        for days in (estimated, routed):
            self.assertEqual(days[0].items[0].start_time, "13:45")
            hotel = days[0].items[-1]
            self.assertEqual(hotel.item_type, "ACCOMMODATION")
            self.assertGreaterEqual(hotel.start_time, "15:00")
            self.assertEqual(hotel.end_time, "21:00")
            self.assertNotEqual(days[1].items[-1].item_type, "ACCOMMODATION")
            self.assertFalse(any(a.item_type == b.item_type == "RESTAURANT"
                                 for day in days for a, b in zip(day.items, day.items[1:])))

    def test_required_restaurant_is_kept_and_two_required_meals_need_replanning(self):
        body, pool, selected = self.scenario()
        for order, index in enumerate((6, 1), 1):
            place = pool[index]
            place.is_required = True
            body.required_places.append(RequiredPlace(
                **place.model_dump(exclude={"is_required", "source_category"}), order=order,
            ))
            if index == 6:
                completed = complete_selection(body, selected, pool)
                ids = [item.provider_place_id for item in completed.days[0].items]
                self.assertIn("6", ids)
                self.assertNotIn("1", ids)
            else:
                with self.assertRaisesRegex(InvalidModelOutput, "required restaurants"):
                    complete_selection(body, selected, pool)

    def test_validator_rejects_consecutive_meals_and_short_hotel_visit(self):
        body, pool, selected = self.scenario()
        with self.assertRaisesRegex(InvalidModelOutput, "restaurants must not be consecutive"):
            validate_itinerary(body, schedule_selection(body, selected, pool), pool)
        fixed = complete_selection(body, selected, pool)
        generated = schedule_selection(body, fixed, pool)
        generated.days[0].items[-1].stay_minutes = 75
        with self.assertRaisesRegex(InvalidModelOutput, "accommodation must cover"):
            validate_itinerary(body, generated, pool)

    def test_arrival_buffer_applies_once_and_does_not_escape_short_day(self):
        body, _, _ = self.scenario()
        for pace, first_start in (("RELAXED", "13:45"), ("BALANCED", "13:30"), ("PACKED", "13:15")):
            body.preference.pace_type = pace
            windows = day_windows(body)
            self.assertEqual(windows[0]["start"], first_start)
            self.assertEqual(windows[1]["start"], "09:00")
        body.preference.pace_type = "RELAXED"
        body.duration.departure_datetime = body.duration.arrival_datetime.replace(minute=20)
        self.assertEqual(day_windows(body)[0]["available_minutes"], 0)

    async def test_early_hotel_is_rest_until_end_not_a_75_minute_attraction(self):
        body = request(None, "CAR")
        pool = places()
        selected = selection()
        generated = schedule_selection(body, selected, pool)
        hotel = generated.days[0].items[-1]
        self.assertGreaterEqual(hotel.start_time, "15:00")
        start = datetime.fromisoformat(f"2026-09-19T{hotel.start_time}")
        self.assertEqual(hotel.stay_minutes, int((datetime(2026, 9, 19, 21) - start).total_seconds() / 60))
        validate_itinerary(body, generated, pool)

    def test_short_departure_day_can_be_empty_after_transfer_exceeds_window(self):
        body = request(None, "CAR")
        body.duration.departure_datetime = datetime(2026, 9, 20, 10, tzinfo=KST)
        pool = places()
        selected = selection()
        selected.days[-1].items = []
        self.assertEqual((day_windows(body)[-1]["available_minutes"],
                          day_windows(body)[-1]["min_items"]), (60, 0))
        days = validate_itinerary(body, schedule_selection(body, selected, pool), pool)
        self.assertEqual(days[-1].items, [])

    def test_short_single_day_adds_one_visit_when_it_fits(self):
        body = request(None, "CAR")
        body.preference.pace_type = "PACKED"
        body.duration.arrival_datetime = datetime(2026, 9, 19, 8, 45, tzinfo=KST)
        body.duration.departure_datetime = datetime(2026, 9, 19, 9, 45, tzinfo=KST)
        pool = [place for place in places() if place.category != "숙소"]
        selected = ModelSelection(title="짧은 여행", days=[
            {"date": "2026-09-19", "items": []},
        ])
        self.assertEqual(day_windows(body)[0]["min_items"], 0)
        completed = complete_selection(body, selected, pool, places_only=True)
        self.assertEqual(len(completed.days[0].items), 1)
        validate_places_selection(body, completed, pool)
        self.assertEqual(len(validate_itinerary(
            body, schedule_selection(body, completed, pool), pool,
        )[0].items), 1)

    def test_time_trim_replaces_too_long_tour_with_feasible_restaurant(self):
        body = request(None, "CAR")
        body.duration.arrival_datetime = datetime(2026, 9, 19, 8, 15, tzinfo=KST)
        body.duration.departure_datetime = datetime(2026, 9, 19, 10, tzinfo=KST)
        pool = [place for place in places() if place.category != "숙소"]
        selected = ModelSelection(title="한 시간 여행", days=[
            {"date": "2026-09-19", "items": [{"provider_place_id": "0"}]},
        ])
        self.assertEqual(day_windows(body)[0]["available_minutes"], 60)
        trimmed = trim_places_to_time(body, selected, pool)
        self.assertEqual(trimmed.days[0].items[0].provider_place_id, "1")
        validate_places_selection(body, trimmed, pool)
        validate_itinerary(body, schedule_selection(body, trimmed, pool), pool)

    def test_trip_too_short_for_any_visit_fails_before_model_call(self):
        body = request(None, "CAR")
        body.preference.pace_type = "PACKED"
        body.duration.arrival_datetime = datetime(2026, 9, 19, 8, 45, tzinfo=KST)
        body.duration.departure_datetime = datetime(2026, 9, 19, 9, 20, tzinfo=KST)
        with self.assertRaises(GenerationFailed) as caught:
            validate_generation_window(body)
        self.assertEqual(caught.exception.reason, "insufficient_trip_time")

    async def test_accommodation_stage_accepts_empty_short_departure_day(self):
        body = request(None, "CAR")
        body.duration.departure_datetime = datetime(2026, 9, 20, 10, tzinfo=KST)
        pool = places()
        selected = selection()
        selected.days[0].items = selected.days[0].items[:2]
        selected.days[-1].items = []
        client = type("FakePlaces", (), {})()
        client.accommodations = AsyncMock(return_value=[pool[2]])
        combined, complete_pool = await recommend_accommodations(body, selected, pool, client)
        days = validate_itinerary(body, schedule_selection(body, combined, complete_pool), complete_pool)
        self.assertEqual(days[-1].items, [])
        self.assertEqual(days[0].items[-1].item_type, "ACCOMMODATION")

    def test_255_minute_day_drops_optional_visit_when_pair_needs_274(self):
        body = request(None, "CAR")
        body.duration.arrival_datetime = datetime(2026, 9, 19, 8, 15, tzinfo=KST)
        body.duration.departure_datetime = datetime(2026, 9, 19, 13, 15, tzinfo=KST)
        pool = places()
        selected = ModelSelection(title="짧은 여행", days=[{
            "date": "2026-09-19", "items": [
                {"provider_place_id": "0"}, {"provider_place_id": "1"},
            ],
        }])
        window = day_windows(body)[0]
        self.assertEqual((window["available_minutes"], window["min_items"]), (255, 2))
        with patch("ai_service.features.travel_minutes", return_value=124):
            self.assertEqual(minimum_day_minutes(pool[:2], window,
                {"관광": [90, 150], "식당": [60, 90], "숙소": [60, 90]}), 274)
            trimmed = trim_places_to_time(body, selected, pool)
            self.assertEqual(len(trimmed.days[0].items), 1)
            validate_places_selection(body, trimmed, pool)
            days = validate_itinerary(body, schedule_selection(body, trimmed, pool), pool)
        self.assertEqual(len(days[0].items), 1)

    def test_short_day_requests_replanning_when_a_closer_pair_exists(self):
        body = request(None, "CAR")
        body.duration.arrival_datetime = datetime(2026, 9, 19, 8, 15, tzinfo=KST)
        body.duration.departure_datetime = datetime(2026, 9, 19, 13, 15, tzinfo=KST)
        pool = places()
        selected = ModelSelection(title="가까운 후보 재시도", days=[{
            "date": "2026-09-19", "items": [
                {"provider_place_id": "0"}, {"provider_place_id": "1"},
            ],
        }])

        def travel(first, second, mode):
            return 5 if {first.provider_place_id, second.provider_place_id} == {"3", "4"} else 124

        with patch("ai_service.features.travel_minutes", side_effect=travel):
            trimmed = trim_places_to_time(body, selected, pool)
            with self.assertRaisesRegex(InvalidModelOutput, "select 2..|tourist place and a restaurant"):
                validate_places_selection(body, trimmed, pool)

    async def test_short_day_uses_feasible_candidates_after_two_bad_model_choices(self):
        body = request(None, "CAR")
        body.duration.arrival_datetime = datetime(2026, 9, 19, 8, 15, tzinfo=KST)
        body.duration.departure_datetime = datetime(2026, 9, 19, 13, 15, tzinfo=KST)
        pool = [place for place in places() if place.category != "숙소"]
        selected = ModelSelection(title="너무 먼 장소", days=[{
            "date": "2026-09-19", "items": [
                {"provider_place_id": "0"}, {"provider_place_id": "1"},
            ],
        }])
        planner = type("FakePlanner", (), {})()
        planner.generate = AsyncMock(return_value=selected)

        def travel(first, second, mode):
            return 5 if {first.provider_place_id, second.provider_place_id} == {"3", "4"} else 124

        with patch("ai_service.features.travel_minutes", side_effect=travel):
            result = await recommend_places(body, pool, planner)
            validate_itinerary(body, schedule_selection(body, result, pool), pool)
        self.assertEqual([item.provider_place_id for item in result.days[0].items], ["3", "4"])
        self.assertEqual(planner.generate.await_count, 2)

    def test_required_visit_moves_from_hotel_only_arrival_day_to_next_day(self):
        body = request(None, "CAR")
        body.preference.pace_type = "BALANCED"
        body.headcount = 10
        body.duration.arrival_datetime = datetime(2026, 9, 19, 18, 30, tzinfo=KST)
        body.duration.departure_datetime = datetime(2026, 9, 20, 13, tzinfo=KST)
        pool = places()
        pool[1].is_required = True
        body.required_places.append(RequiredPlace(
            **pool[1].model_dump(exclude={"is_required", "source_category"}), order=1,
        ))
        selected = ModelSelection(title="필수 장소 이동", days=[
            {"date": "2026-09-19", "items": [{"provider_place_id": "1"}]},
            {"date": "2026-09-20", "items": []},
        ])
        fallback = fallback_multi_day(body, pool, selected)
        self.assertIsNotNone(fallback)
        self.assertEqual([item.provider_place_id for item in fallback.days[0].items], [])
        self.assertIn("1", [item.provider_place_id for item in fallback.days[1].items])
        validate_places_selection(body, fallback, pool)

    def test_multi_day_fallback_restores_required_visit_omitted_by_model(self):
        body = request(None, "CAR")
        pool = places()
        pool[0].is_required = True
        body.required_places.append(RequiredPlace(
            **pool[0].model_dump(exclude={"is_required", "source_category"}), order=1,
        ))
        selected = ModelSelection(title="누락된 필수 장소", days=[
            {"date": "2026-09-19", "items": []},
            {"date": "2026-09-20", "items": []},
        ])
        fallback = fallback_multi_day(body, pool, selected)
        self.assertIsNotNone(fallback)
        self.assertIn("0", [item.provider_place_id
                            for day in fallback.days for item in day.items])
        validate_places_selection(body, fallback, pool)

    def test_feasible_restaurant_replacement_keeps_category_requirement(self):
        body = request(None, "CAR")
        body.duration.arrival_datetime = datetime(2026, 9, 19, 8, 15, tzinfo=KST)
        body.duration.departure_datetime = datetime(2026, 9, 19, 13, 15, tzinfo=KST)
        selected = ModelSelection(title="식당 교체", days=[{
            "date": "2026-09-19", "items": [
                {"provider_place_id": "0"}, {"provider_place_id": "3"},
            ],
        }])
        with patch("ai_service.features.travel_minutes", return_value=5):
            with self.assertRaisesRegex(InvalidModelOutput, "include a tourist place and a restaurant"):
                validate_places_selection(body, selected, places())

    async def test_lodging_retries_after_dropping_one_optional_visit(self):
        body = request(None, "CAR")
        pool = places()
        for order, place in enumerate(pool[:2], 1):
            place.is_required = True
            body.required_places.append(RequiredPlace(
                **place.model_dump(exclude={"is_required", "source_category"}), order=order,
            ))
        selected = ModelSelection(title="숙소 재배치", days=[
            {"date": "2026-09-19", "items": [
                {"provider_place_id": "0"}, {"provider_place_id": "1"},
                {"provider_place_id": "3"},
            ]},
            {"date": "2026-09-20", "items": [
                {"provider_place_id": "4"}, {"provider_place_id": "5"},
            ]},
        ])
        client = type("FakePlaces", (), {})()
        client.accommodations = AsyncMock(return_value=[pool[2]])
        with patch("ai_service.features.travel_minutes", return_value=180):
            combined, complete_pool = await recommend_accommodations(body, selected, pool, client)
            days = validate_itinerary(body, schedule_selection(body, combined, complete_pool), complete_pool)
        self.assertEqual(len(combined.days[0].items), 3)
        self.assertEqual([item.provider_place_id for item in combined.days[0].items[:2]], ["0", "1"])
        self.assertEqual(combined.days[0].items[-1].provider_place_id, "2")
        self.assertEqual(days[0].items[-1].item_type, "ACCOMMODATION")
        client.accommodations.assert_awaited_once()

    async def test_lodging_can_trim_next_short_day_when_hotel_transfer_is_too_long(self):
        body = request(None, "CAR")
        body.duration.departure_datetime = datetime(2026, 9, 20, 10, tzinfo=KST)
        pool = places()
        selected = ModelSelection(title="짧은 출발일", days=[
            {"date": "2026-09-19", "items": [
                {"provider_place_id": "0"}, {"provider_place_id": "1"},
            ]},
            {"date": "2026-09-20", "items": [{"provider_place_id": "3"}]},
        ])
        client = type("FakePlaces", (), {})()
        client.accommodations = AsyncMock(return_value=[pool[2]])
        with patch("ai_service.features.travel_minutes", return_value=180):
            combined, complete_pool = await recommend_accommodations(body, selected, pool, client)
            days = validate_itinerary(
                body, schedule_selection(body, combined, complete_pool), complete_pool,
            )
        self.assertEqual(days[-1].items, [])
        self.assertEqual(days[0].items[-1].item_type, "ACCOMMODATION")
