from datetime import datetime
import unittest

import httpx

from ai_service.config import Settings
from ai_service.errors import GenerationFailed
from ai_service.features import build_context, generate_itinerary, schedule_selection, validate_itinerary
from ai_service.pipeline import connect_routes
from ai_service.places import travel_minutes
from ai_service.routing import KakaoRoutes
from ai_service.schemas import ItineraryRequest, KST, ModelSelection, Place
from ai_service.transport import resolve_day_transports


EXTRA = "이동 시간이 너무 길지 않게 여유로운 일정으로 추천해주세요. 또한 첫날에는 자동차를 이용할거고 둘째날에는 버스를 이용할거라서 첫날에는 대중교통을 추천해주지 않아도 됩니다."


def request(extra=EXTRA, default="PUBLIC_TRANSPORT"):
    return ItineraryRequest.model_validate({
        "generation_job_id": 1, "region": {"region_id": 2, "full_name": "부산광역시"},
        "duration": {"arrival_datetime": "2026-09-19T10:00:00+09:00", "departure_datetime": "2026-09-20T18:00:00+09:00"},
        "headcount": 2, "companion_type": "COUPLE",
        "preference": {"pace_type": "RELAXED", "transport_type": default,
                       "themes": ["NATURE", "FOOD"], "extra_request": extra},
        "required_places": [],
    })


def http_request(body=None):
    value = (body or request()).model_dump(mode="json")
    region = value.pop("region")
    value["region_id"] = region["region_id"]
    value["region_name"] = region["full_name"]
    value["travel_plan_id"] = value.get("travel_plan_id") or 10
    value.update(value.pop("duration"))
    value.pop("generation_job_id")
    value["preference"]["distance_preference"] = 50
    return value


def places():
    return [Place(provider_place_id=str(i), place_name=f"테스트 장소 {i}", address="부산",
                  latitude=35.16 + i * .001, longitude=129.06 + i * .001,
                  category=category, source_category=category)
            for i, category in enumerate(["관광", "식당", "숙소", "관광", "식당", "관광"])]


def selection():
    return ModelSelection(title="부산 여행", days=[
        {"date": date, "items": [{"provider_place_id": str(i)} for i in ids]}
        for date, ids in [("2026-09-19", range(3)), ("2026-09-20", range(3, 6))]
    ])


def transit_payload(origin, destination, modes=("SUBWAY", "BUS")):
    routes = []
    for mode in modes:
        seconds = 60 if mode == "SUBWAY" else 600
        routes.append({"properties": {"totalTime": seconds, "totalDistance": 1000},
                       "steps": [{"properties": {"type": mode, "time": seconds, "distance": 1000,
                                                 "vehicles": [{"name": "141(심야)" if mode == "BUS" else "2호선", "type": "일반"}],
                                                 "stops": [{"name": "출발"}, {"name": "도착"}]},
                                  "path": {"points": [[origin.longitude, origin.latitude], [destination.longitude, destination.latitude]]}}]})
    return {"status": "OK", "routes": routes}


class TransportTests(unittest.TestCase):
    def test_three_day_ranges_override_default_and_preserve_request(self):
        for default in ("CAR", "PUBLIC_TRANSPORT"):
            for span in ("2~3일차", "2일차~3일차", "2일차부터 3일차까지", "2～3일차", "2-3일차"):
                with self.subTest(default=default, span=span):
                    body = request(f"제주도 3일 일정 중 1일차에는 렌터카를 이용하고 {span}에는 대중교통을 이용한다", default)
                    body.duration.departure_datetime = datetime(2026, 9, 21, 18, tzinfo=KST)
                    before = body.model_dump()
                    self.assertEqual(resolve_day_transports(body), {
                        "2026-09-19": "CAR", "2026-09-20": "PUBLIC_TRANSPORT", "2026-09-21": "PUBLIC_TRANSPORT",
                    })
                    self.assertEqual([w["route_mode"] for w in build_context(body, places())["day_windows"]],
                                     ["CAR", "PUBLIC_TRANSPORT", "PUBLIC_TRANSPORT"])
                    self.assertEqual(body.model_dump(), before)

    def test_invalid_day_range_is_rejected(self):
        for text in ("0~2일차 자차", "2~1일차 자차", "1~3일차 자차"):
            with self.subTest(text=text), self.assertRaises(GenerationFailed):
                resolve_day_transports(request(text))

    def test_exact_user_request_and_negative_clause(self):
        self.assertEqual(resolve_day_transports(request()), {"2026-09-19": "CAR", "2026-09-20": "BUS"})

    def test_rental_car_ignores_unneeded_transit(self):
        for word in ("렌트카", "렌터카", "자동차", "자차"):
            with self.subTest(word=word):
                actual = resolve_day_transports(request(f"첫날 {word}를 이용하므로 대중교통은 상관없습니다."))
                self.assertEqual(actual, {"2026-09-19": "CAR", "2026-09-20": "PUBLIC_TRANSPORT"})

    def test_default_and_day_number_and_iso_date(self):
        self.assertEqual(set(resolve_day_transports(request(None)).values()), {"PUBLIC_TRANSPORT"})
        self.assertEqual(resolve_day_transports(request("1일차 렌터카, 2026-09-20 버스")), resolve_day_transports(request()))
        self.assertEqual(resolve_day_transports(request("둘째 날에는 버스", "CAR")), resolve_day_transports(request()))

    def test_negated_car_is_not_selected(self):
        actual = resolve_day_transports(request("첫날 자동차를 이용하지 않고 버스를 이용합니다."))
        self.assertEqual(actual["2026-09-19"], "BUS")

    def test_shared_negative_transit_predicate(self):
        actual = resolve_day_transports(request("첫날 렌터카를 이용하므로 버스나 지하철은 필요 없습니다."))
        self.assertEqual(actual["2026-09-19"], "CAR")

    def test_conflicting_or_out_of_range_day_is_rejected(self):
        for text in ("첫날 자동차, 첫날 버스", "3일차 자동차"):
            with self.subTest(text=text), self.assertRaises(GenerationFailed):
                resolve_day_transports(request(text))

    def test_context_and_local_schedule_use_same_modes(self):
        body, pool = request(), places()
        context = build_context(body, pool)
        self.assertEqual([w["route_mode"] for w in context["day_windows"]], ["CAR", "BUS"])
        matrices = context["travel_edges"]["minutes_by_date"]
        self.assertEqual(matrices["2026-09-19"][0][1], travel_minutes(pool[0], pool[1], "CAR"))
        self.assertEqual(matrices["2026-09-20"][0][1], travel_minutes(pool[0], pool[1], "PUBLIC_TRANSPORT"))
        days = validate_itinerary(body, schedule_selection(body, selection(), pool), pool)
        gap = (datetime.strptime(days[0].items[1].start_time, "%H:%M") - datetime.strptime(days[0].items[0].end_time, "%H:%M")).total_seconds() / 60
        self.assertGreaterEqual(gap, travel_minutes(pool[0], pool[1], "CAR"))
        gap = (datetime.strptime(days[1].items[0].start_time, "%H:%M") - datetime.strptime("09:00", "%H:%M")).total_seconds() / 60
        self.assertGreaterEqual(gap, travel_minutes(pool[2], pool[3], "PUBLIC_TRANSPORT"))

    def test_public_schema_has_no_new_transport_fields(self):
        body = request()
        before = body.model_dump()
        resolve_day_transports(body)
        self.assertEqual(body.model_dump(), before)
        self.assertNotIn("day_transports", ItineraryRequest.model_json_schema()["properties"])


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_day_range_drives_actual_route_calls(self):
        body = request("1일차 렌터카, 2~3일차 대중교통", default="CAR")
        body.duration.departure_datetime = datetime(2026, 9, 21, 18, tzinfo=KST)
        pool = self.pool + [
            Place(provider_place_id=str(i), place_name=f"테스트 장소 {i}", address="부산",
                  latitude=35.16 + i * .001, longitude=129.06 + i * .001, category=cat, source_category=cat)
            for i, cat in ((6, "관광"), (7, "식당"), (8, "관광"))
        ]
        selected = ModelSelection(title="3일 여행", days=[
            {"date": f"2026-09-{19 + n}", "items": [{"provider_place_id": str(i)} for i in ids]}
            for n, ids in enumerate(((0, 1, 2), (3, 4, 2), (6, 7, 8)))
        ])
        result = await connect_routes(body, selected, pool, "test", self.router)
        for day in result.itinerary.days:
            for item in day.items:
                route = item.route_from_previous
                if route is not None:
                    self.assertEqual(route.transport_type, "CAR" if day.day_number == 1 else "PUBLIC_TRANSPORT")
                    self.assertEqual(bool(route.legs), day.day_number != 1)
        self.assertTrue(self.calls)
        self.assertTrue(all(req.url.path.endswith("/publictraffic") for req in self.calls))
        for day in result.itinerary.days[1:]:
            self.assertEqual(day.items[0].route_from_previous.transport_type, "PUBLIC_TRANSPORT")

    async def asyncSetUp(self):
        self.calls = []
        self.pool = places()

        def handle(req):
            self.calls.append(req)
            params = req.url.params
            origin = type("Point", (), {"longitude": float(params["start_x"]), "latitude": float(params["start_y"])})()
            dest = type("Point", (), {"longitude": float(params["end_x"]), "latitude": float(params["end_y"])})()
            return httpx.Response(200, json=transit_payload(origin, dest))

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.router = KakaoRoutes(self.client, Settings(kakao_rest_api_key="test-only"))
        self.departure = datetime(2026, 9, 19, 10, tzinfo=KST)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_car_has_no_transit_instructions_or_provider_call(self):
        router = KakaoRoutes(self.client, Settings())
        route = await router.route(*self.pool[:2], self.departure, "CAR")
        self.assertEqual(route.transport_type, "CAR")
        self.assertEqual(route.legs, [])
        self.assertEqual(route.provider, "GEOGRAPHIC_ESTIMATE")
        self.assertTrue(route.is_estimated)
        self.assertFalse(self.calls)

    async def test_bus_excludes_faster_subway(self):
        route = await self.router.route(*self.pool[:2], self.departure, "BUS")
        self.assertEqual([leg.mode for leg in route.legs], ["BUS"])
        self.assertEqual(route.legs[0].bus_number, "141(심야)")
        self.assertEqual(route.duration_minutes, 10)
        default = await self.router.route(*self.pool[:2], self.departure, "PUBLIC_TRANSPORT")
        self.assertEqual([leg.mode for leg in default.legs], ["SUBWAY"])

    async def test_bus_without_matching_route_fails_instead_of_using_subway(self):
        for payload in (transit_payload(*self.pool[:2], modes=("SUBWAY",)), {"status": "NO_RESULTS"}):
            async def get(*args):
                return payload
            self.router._get = get
            with self.assertRaises(GenerationFailed):
                await self.router.route(*self.pool[:2], self.departure, "BUS")

    async def test_sync_and_sse_routes_use_destination_day_including_overnight(self):
        pool = self.pool

        class Places:
            async def collect(self, body):
                return pool

        class Planner:
            settings = Settings()
            async def generate(self, context, feedback):
                return selection()

        sync = await generate_itinerary(request(), Places(), Planner(), self.router)
        stream_routes = await connect_routes(request(), selection(), pool, "test", self.router)
        for result in (sync, stream_routes.itinerary):
            self.assertIsNone(result.days[0].items[0].route_from_previous)
            for item in result.days[0].items[1:]:
                self.assertEqual(item.route_from_previous.transport_type, "CAR")
                self.assertNotIn("legs", item.route_from_previous.model_dump())
            for item in result.days[1].items:
                self.assertEqual(item.route_from_previous.transport_type, "PUBLIC_TRANSPORT")
                self.assertEqual(item.route_from_previous.duration_minutes, 10)  # BUS, not faster SUBWAY
                self.assertEqual(item.route_from_previous.legs[0].vehicle_number, "141(심야)")
                self.assertEqual(item.route_from_previous.legs[0].start.name, "출발")
                self.assertEqual(item.route_from_previous.legs[0].end.name, "도착")
        overnight = stream_routes.itinerary.days[1].items[0].route_from_previous
        self.assertEqual(overnight.duration_minutes, 10)
        self.assertTrue(any(float(req.url.params["start_y"]) == pool[2].latitude
                            and float(req.url.params["end_y"]) == pool[3].latitude for req in self.calls))

    async def test_global_car_still_allows_day_two_bus(self):
        result = await connect_routes(request(default="CAR"), selection(), self.pool, "test", self.router)
        self.assertEqual(result.itinerary.days[1].items[0].route_from_previous.duration_minutes, 10)

    async def test_all_car_uses_no_external_routes(self):
        result = await connect_routes(request(None, "CAR"), selection(), self.pool, "test", self.router)
        routes = [item.route_from_previous for day in result.itinerary.days for item in day.items if item.route_from_previous]
        self.assertTrue(routes)
        self.assertTrue(all(r.transport_type == "CAR" and "legs" not in r.model_dump() for r in routes))
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
