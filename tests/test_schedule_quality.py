from datetime import datetime
import unittest

import httpx

from ai_service.config import Settings
from ai_service.errors import InvalidModelOutput
from ai_service.features import complete_selection, day_windows, schedule_selection, validate_itinerary
from ai_service.routing import KakaoRoutes, schedule_with_routes
from ai_service.schemas import RequiredPlace
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
