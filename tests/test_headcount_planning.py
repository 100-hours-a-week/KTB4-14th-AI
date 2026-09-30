"""인원수만 바꾼 동일 요청에서 일정 밀도·동선·경로 선택을 비교한다."""

from datetime import datetime
import unittest

import httpx

from ai_service.config import Settings
from ai_service.features import (
    complete_selection, day_windows, optimize_group_order, select_candidates,
)
from ai_service.group_policy import movement_buffer_minutes, transfer_penalty_seconds
from ai_service.routing import KakaoRoutes, schedule_with_routes
from ai_service.schemas import ModelSelection, Place, RequiredPlace
from test_day_transport import request


def body_for(headcount, *, pace="PACKED", transport="PUBLIC_TRANSPORT"):
    body = request(None, transport)
    body.headcount = headcount
    body.preference.pace_type = pace
    body.preference.distance_preference = 100
    body.duration.arrival_datetime = datetime(2026, 9, 19, 9)
    body.duration.departure_datetime = datetime(2026, 9, 19, 21)
    return body


def place(pid, longitude, category="관광", *, required=False):
    return Place(
        provider_place_id=pid, place_name=pid, address="부산",
        latitude=35.1, longitude=longitude, category=category,
        source_category=category, is_required=required,
    )


def selection(*ids):
    return ModelSelection(title="부산 여행", days=[{
        "date": "2026-09-19", "items": [{"provider_place_id": pid} for pid in ids],
    }])


def transit_payload(origin, destination):
    def option(modes, seconds, meters):
        steps = []
        for index, mode in enumerate(modes):
            start = index / len(modes)
            end = (index + 1) / len(modes)
            points = [
                [origin.longitude + (destination.longitude - origin.longitude) * part,
                 origin.latitude + (destination.latitude - origin.latitude) * part]
                for part in (start, end)
            ]
            steps.append({"properties": {
                "type": mode, "time": seconds // len(modes), "distance": meters // len(modes),
                "stops": [{"name": f"출발{index}"}, {"name": f"도착{index}"}],
                "vehicles": [{"name": f"노선{index}", "type": "일반"}],
            }, "path": {"points": points}})
        return {"properties": {"totalTime": seconds, "totalDistance": meters}, "steps": steps}

    return {"status": "OK", "routes": [
        option(("BUS", "SUBWAY", "BUS"), 600, 12000),
        option(("SUBWAY",), 1020, 9000),
    ]}


class HeadcountPlanningTests(unittest.TestCase):
    def test_policy_boundaries_and_packed_density(self):
        expected = {
            1: (0, 8, 6), 2: (0, 8, 6), 4: (0, 8, 6),
            8: (0, 8, 6), 9: (0, 8, 6),
            10: (5, 7, 5), 15: (5, 7, 5), 19: (5, 7, 5),
            20: (10, 6, 4), 30: (10, 6, 4),
        }
        for count, (buffer, maximum, minimum) in expected.items():
            with self.subTest(headcount=count):
                window = day_windows(body_for(count))[0]
                self.assertEqual((window.get("group_transfer_buffer_minutes", 0), window["max_items"], window["min_items"]),
                                 (buffer, maximum, minimum))
        self.assertEqual(movement_buffer_minutes(9), 0)
        self.assertEqual(movement_buffer_minutes(10), 5)
        self.assertEqual(movement_buffer_minutes(19), 5)
        self.assertEqual(movement_buffer_minutes(20), 10)

    def test_large_group_reduces_packed_optional_places_without_dropping_required(self):
        pool = [place(str(i), 129.0 + i * 0.001, "식당" if i in (2, 5) else "관광",
                      required=i in (0, 3)) for i in range(8)]
        selected = selection(*(str(i) for i in range(8)))
        for count, expected_count in ((2, 8), (30, 6)):
            body = body_for(count)
            body.required_places = [RequiredPlace(
                **pool[i].model_dump(exclude={"is_required", "source_category"}), order=order,
            ) for order, i in enumerate((0, 3), 1)]
            result = complete_selection(body, selected, pool, places_only=True)
            ids = [item.provider_place_id for item in result.days[0].items]
            self.assertEqual(len(ids), expected_count)
            self.assertLess(ids.index("0"), ids.index("3"))

    def test_same_places_are_grouped_and_required_order_stays_intact(self):
        pool = [place("A", 129.00, required=True), place("B", 129.20, "식당"),
                place("C", 129.002), place("D", 129.202, required=True)]
        selected = selection("A", "B", "C", "D")
        orders = {}
        for count in (2, 8, 10, 15, 20, 30):
            body = body_for(count)
            body.required_places = [RequiredPlace(
                **pool[i].model_dump(exclude={"is_required", "source_category"}), order=order,
            ) for order, i in enumerate((0, 3), 1)]
            result = optimize_group_order(body, selected, pool)
            orders[count] = [item.provider_place_id for item in result.days[0].items]
            self.assertLess(orders[count].index("A"), orders[count].index("D"))
        self.assertEqual(orders[2], ["A", "B", "C", "D"])
        self.assertEqual(orders[30], ["A", "C", "B", "D"])

    def test_group_candidate_pool_keeps_close_options_even_at_distance_100(self):
        pool = [place("anchor", 129.0)] + [place(f"far{i}", 129.2 + i * 0.01) for i in range(8)]
        pool += [place("near1", 129.001), place("near2", 129.002)]
        small = {p.provider_place_id for p in select_candidates(body_for(2), pool)}
        large = {p.provider_place_id for p in select_candidates(body_for(30), pool)}
        self.assertNotIn("near2", small)
        self.assertIn("near2", large)


class HeadcountRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_options_use_fewer_transfers_for_groups(self):
        origin, destination = place("A", 129.0), place("B", 129.01)
        payload = transit_payload(origin, destination)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=payload)
        )) as client:
            router = KakaoRoutes(client, Settings(kakao_rest_api_key="test"))
            for count, expected_legs, expected_minutes in (
                (2, 3, 10), (8, 3, 10), (10, 1, 17), (15, 1, 17), (20, 1, 17), (30, 1, 17),
            ):
                with self.subTest(headcount=count):
                    route = await router.route(origin, destination, datetime(2026, 9, 19, 10),
                                               "PUBLIC_TRANSPORT", headcount=count)
                    self.assertEqual(len(route.legs), expected_legs)
                    self.assertEqual(route.duration_minutes, expected_minutes)
        self.assertLess(transfer_penalty_seconds(9), transfer_penalty_seconds(10))
        self.assertLess(transfer_penalty_seconds(19), transfer_penalty_seconds(20))

    async def test_actual_route_duration_is_unchanged_and_buffer_only_delays_next_start(self):
        pool = [place("A", 129.0), place("B", 129.001, "식당"), place("C", 129.002)]
        selected = selection("A", "B", "C")

        def respond(req):
            longitude_a = float(req.url.params["start_x"])
            longitude_b = float(req.url.params["end_x"])
            origin = place("from", longitude_a)
            destination = place("to", longitude_b)
            return httpx.Response(200, json={"status": "OK", "routes": [
                transit_payload(origin, destination)["routes"][1]
            ]})

        summaries = {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            router = KakaoRoutes(client, Settings(kakao_rest_api_key="test"))
            for count in (2, 30):
                body = body_for(count, pace="RELAXED")
                result = await schedule_with_routes(body, selected, pool, "test", router)
                items = result.itinerary.days[0].items
                route = items[1].route_from_previous
                end = datetime.strptime(items[0].end_time, "%H:%M")
                start = datetime.strptime(items[1].start_time, "%H:%M")
                summaries[count] = (route.duration_minutes, int((start - end).total_seconds() / 60))
        self.assertEqual(summaries[2], (17, 17))
        self.assertEqual(summaries[30], (17, 27))

    async def test_packed_large_group_keeps_pace_but_has_fewer_less_crowded_visits(self):
        pool = [place(str(i), 129.0 + i * 0.001, "식당" if i in (2, 5) else "관광")
                for i in range(8)]
        selected = selection(*(str(i) for i in range(8)))

        def respond(req):
            origin = place("from", float(req.url.params["start_x"]))
            destination = place("to", float(req.url.params["end_x"]))
            return httpx.Response(200, json={"status": "OK", "routes": [
                transit_payload(origin, destination)["routes"][1]
            ]})

        results = {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            router = KakaoRoutes(client, Settings(kakao_rest_api_key="test"))
            for count in (2, 30):
                body = body_for(count)
                chosen = complete_selection(body, selected, pool, places_only=True)
                itinerary = (await schedule_with_routes(body, chosen, pool, "test", router)).itinerary
                items = itinerary.days[0].items
                gaps = []
                for before, after in zip(items, items[1:]):
                    end = datetime.strptime(before.end_time, "%H:%M")
                    start = datetime.strptime(after.start_time, "%H:%M")
                    gaps.append(int((start - end).total_seconds() / 60))
                    self.assertEqual(after.route_from_previous.duration_minutes, 17)
                results[count] = (len(items), min(gaps))
        self.assertEqual(results[2], (8, 17))
        self.assertEqual(results[30], (6, 27))


if __name__ == "__main__":
    unittest.main()
