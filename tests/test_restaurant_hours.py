from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from ai_service.config import Settings
from ai_service.errors import GenerationFailed, InvalidModelOutput, ServiceUnavailable
from ai_service.features import make_itinerary_response, schedule_selection, validate_itinerary
from ai_service.pipeline import connect_routes, replace_closed_restaurant
from ai_service.places import KakaoPlaces
from ai_service.restaurant_hours import RestaurantHours, attach_verified_hours
from ai_service.routing import KakaoRoutes
from ai_service.schemas import ModelSelection
from test_day_transport import places, request, selection


def hours(**overrides):
    weekly = {day: [["09:00", "22:00"]] for day in
              ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    weekly.update(overrides)
    return RestaurantHours({
        "source_url": "https://map.naver.com/p/entry/place/example",
        "weekly": weekly,
    })


class RestaurantHoursTests(unittest.IsolatedAsyncioTestCase):
    def test_break_and_overnight_and_exception(self):
        record = {"source_url": "https://map.naver.com/p/entry/place/example",
                  "weekly": {day: [] for day in
                             ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
                  "exceptions": {"2026-09-19": [["11:00", "14:00"], ["17:00", "02:00"]]}}
        h = RestaurantHours(record)
        self.assertEqual(h.next_start(datetime(2026, 9, 19, 14, 30), 60,
                                      datetime(2026, 9, 19, 23)),
                         datetime(2026, 9, 19, 17))
        self.assertTrue(h.contains(datetime(2026, 9, 20, 0, 30),
                                   datetime(2026, 9, 20, 1, 30)))
        self.assertFalse(h.contains(datetime(2026, 9, 19, 13, 30),
                                    datetime(2026, 9, 19, 14, 30)))

    def test_unknown_optional_restaurant_is_excluded_required_is_rejected(self):
        pool = places()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hours.json"
            path.write_text(json.dumps({"1": {
                "source_url": "https://map.naver.com/p/entry/place/example",
                "weekly": {day: [["09:00", "22:00"]] for day in
                           ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
            }}), encoding="utf-8")
            result = attach_verified_hours(pool, path)
            self.assertIn("1", [p.provider_place_id for p in result])
            self.assertNotIn("4", [p.provider_place_id for p in result])
            self.assertNotIn("_restaurant_hours", result[1].model_dump())
            pool[4].is_required = True
            with self.assertRaises(GenerationFailed) as raised:
                attach_verified_hours(pool, path)
            self.assertEqual(raised.exception.reason, "required_restaurant_hours_missing")
            with self.assertRaises(ServiceUnavailable):
                attach_verified_hours(pool, path.with_name("missing.json"))

    async def test_jeju_collection_uses_verified_hours_without_naver_api(self):
        body = request(None, "CAR")
        body.region.full_name = "제주특별자치도 제주시"
        tourist = {"id": "tour", "place_name": "제주 관광지", "address_name": "제주 제주시 연동",
                   "road_address_name": "제주특별자치도 제주시 연동로 1", "x": "126.50", "y": "33.50",
                   "category_group_code": "AT4"}
        known = {"id": "1387964178", "place_name": "올래국수 본점",
                 "address_name": "제주 제주시 연동", "road_address_name": "제주특별자치도 제주시 귀아랑길 24",
                 "x": "126.50", "y": "33.50", "category_group_code": "FD6"}
        unknown = {**known, "id": "unknown", "place_name": "시간 미확인 식당"}

        def handle(req):
            documents = ([{"address_type": "REGION", "address_name": "제주특별자치도 제주시",
                           "x": "126.50", "y": "33.50"}]
                         if req.url.path.endswith("/address.json") else [tourist, known, unknown])
            return httpx.Response(200, json={"documents": documents})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            collected = await KakaoPlaces(client, Settings(kakao_rest_api_key="test-key")).collect(
                body, include_accommodation=False)
        self.assertEqual({p.provider_place_id for p in collected}, {"tour", "1387964178"})
        self.assertIsNotNone(next(p for p in collected if p.category == "식당")._restaurant_hours)

    def test_schedulers_wait_until_open_and_validate_full_meal(self):
        body = request(None, "CAR")
        pool = places()
        selected = selection()
        pool[1]._restaurant_hours = hours(sat=[["16:00", "18:00"]])
        generated = schedule_selection(body, selected, pool)
        self.assertEqual(generated.days[0].items[1].start_time, "16:00")
        validate_itinerary(body, generated, pool)
        generated.days[0].items[1].start_time = "17:30"
        with self.assertRaisesRegex(InvalidModelOutput, "restaurant_closed"):
            validate_itinerary(body, generated, pool)

    def test_final_json_does_not_expose_restaurant_hours(self):
        body, pool = request(None, "CAR"), places()
        pool[1]._restaurant_hours = hours()
        pool[4]._restaurant_hours = hours()
        generated = schedule_selection(body, selection(), pool)
        days = validate_itinerary(body, generated, pool)
        response = make_itinerary_response(body, generated, days, "test")
        payload = response.model_dump(mode="json")
        self.assertNotIn("restaurant_hours", json.dumps(payload))
        self.assertNotIn("source_url", json.dumps(payload))
        self.assertNotIn("weekly", json.dumps(payload))
        self.assertIn("start_time", payload["days"][0]["items"][1])

    async def test_closed_restaurant_is_replaced_after_selection_and_routing(self):
        body = request(None, "CAR")
        pool = places()
        alternative = pool[1].model_copy(deep=True)
        alternative.provider_place_id = "6"
        alternative.place_name = "대체 식당"
        pool.append(alternative)
        pool[1]._restaurant_hours = hours(sat=[])
        pool[4]._restaurant_hours = hours()
        alternative._restaurant_hours = hours()
        selected = selection()
        only_places = ModelSelection(title=selected.title, days=[
            {"date": day.date, "items": [item.model_dump() for item in day.items
                                       if item.provider_place_id != "2"]}
            for day in selected.days
        ])
        repaired = replace_closed_restaurant(body, only_places, pool)
        self.assertEqual(repaired.days[0].items[1].provider_place_id, "6")
        async with httpx.AsyncClient() as client:
            result = await connect_routes(body, selected, pool, "test",
                                          KakaoRoutes(client, Settings()))
        self.assertEqual(result.itinerary.days[0].items[1].provider_place_id, "6")

    async def test_route_replaces_restaurant_when_waiting_pushes_lodging_out_of_time(self):
        body = request(None, "CAR")
        pool = places()
        alternative = pool[1].model_copy(deep=True)
        alternative.provider_place_id = "6"
        alternative.place_name = "일찍 여는 식당"
        pool.append(alternative)
        pool[1]._restaurant_hours = hours(sat=[["22:00", "23:59"]])
        pool[4]._restaurant_hours = hours()
        alternative._restaurant_hours = hours()

        async with httpx.AsyncClient() as client:
            result = await connect_routes(body, selection(), pool, "test",
                                          KakaoRoutes(client, Settings()))
        self.assertEqual(result.itinerary.days[0].items[1].provider_place_id, "6")

    def test_closed_optional_restaurant_is_removed_when_no_open_alternative(self):
        body = request(None, "CAR")
        pool = places()
        pool[1]._restaurant_hours = hours(sat=[])
        pool[4]._restaurant_hours = hours()
        selected = selection()
        selected.days[0].items = selected.days[0].items[:2]
        repaired = replace_closed_restaurant(body, selected, pool)
        self.assertEqual([item.provider_place_id for item in repaired.days[0].items], ["0"])
        generated = schedule_selection(body, repaired, pool)
        self.assertFalse(any(item.provider_place_id == "1" for item in generated.days[0].items))


if __name__ == "__main__":
    unittest.main()
