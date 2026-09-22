import json
import unittest
from datetime import datetime

import httpx
from fastapi.testclient import TestClient

from ai_service.config import Settings
from ai_service.errors import RoutingUnavailable
from ai_service.main import create_app
from ai_service.routing import KakaoRoutes, summarize_route
from ai_service.schemas import KST
from test_compact_routes import PlaceClient, Planner, all_keys
from test_day_transport import http_request, places, transit_payload


def transfer_payload(origin, destination):
    point = type("Point", (), {
        "latitude": (origin.latitude + destination.latitude) / 2,
        "longitude": (origin.longitude + destination.longitude) / 2,
    })()
    bus = transit_payload(origin, point, ("BUS",))["routes"][0]["steps"][0]
    subway = transit_payload(point, destination, ("SUBWAY",))["routes"][0]["steps"][0]
    bus["properties"]["stops"] = [{"name": n} for n in ("출발 정류장", "중간 정류장", "환승 정류장")]
    subway["properties"]["stops"] = [{"name": n} for n in ("환승역", "중간역", "도착역")]
    return {"status": "OK", "routes": [{
        "properties": {"totalTime": 660, "totalDistance": 2000}, "steps": [bus, subway],
    }]}


class TransitGuidanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_transfer_preserves_boarding_order_without_full_stops_or_coordinates(self):
        origin, destination = places()[:2]
        payload = transfer_payload(origin, destination)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
            details = await KakaoRoutes(client, Settings(kakao_rest_api_key="test")).route(
                origin, destination, datetime(2026, 9, 19, 10, tzinfo=KST), "PUBLIC_TRANSPORT",
            )
        summary = summarize_route(details).model_dump(mode="json")
        self.assertEqual(summary["duration_minutes"], 11)
        self.assertEqual([leg["mode"] for leg in summary["legs"]], ["BUS", "SUBWAY"])
        self.assertEqual(summary["legs"][0]["vehicle_number"], "141(심야)")
        self.assertEqual(summary["legs"][0]["start"], {"name": "출발 정류장", "station_number": None})
        self.assertEqual(summary["legs"][0]["end"], {"name": "환승 정류장", "station_number": None})
        self.assertEqual(summary["legs"][1]["line_name"], "2호선")
        self.assertEqual(summary["legs"][1]["start"], {"name": "환승역", "station_number": None})
        self.assertEqual(summary["legs"][1]["end"], {"name": "도착역", "station_number": None})
        self.assertNotIn("vehicle_number", summary)  # Do not label a transfer as one bus.
        self.assertFalse(all_keys(summary) & {"path", "latitude", "longitude", "stops", "vehicles", "instructions"})
        details.legs[0].start.station_number = "00123"
        details.legs[0].end.station_number = "00456"
        details.legs[0].start.station_id = "provider-internal-id"
        details.legs[1].start.station_id = "subway-internal-id"
        enriched = summarize_route(details).model_dump(mode="json")
        self.assertEqual(enriched["legs"][0]["start"]["station_number"], "00123")
        self.assertEqual(enriched["legs"][0]["end"]["station_number"], "00456")
        self.assertIsNone(enriched["legs"][1]["start"]["station_number"])
        self.assertNotIn("station_id", all_keys(enriched))


    async def test_missing_transit_stations_are_not_filled_with_place_names(self):
        origin, destination = places()[:2]
        for stops in ([], [{"name": "출발", "station_number": None}], [{"name": " "}, {"name": "도착", "station_number": None}]):
            payload = transfer_payload(origin, destination)
            payload["routes"][0]["steps"][0]["properties"]["stops"] = stops
            with self.subTest(stops=stops):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
                    with self.assertRaises(RoutingUnavailable):
                        await KakaoRoutes(client, Settings(kakao_rest_api_key="test")).route(
                            origin, destination, datetime(2026, 9, 19, 10, tzinfo=KST), "PUBLIC_TRANSPORT",
                        )

    async def test_single_subway_has_line_and_stations_but_no_invented_train_number(self):
        origin, destination = places()[:2]
        payload = transit_payload(origin, destination, ("SUBWAY",))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
            details = await KakaoRoutes(client, Settings(kakao_rest_api_key="test")).route(
                origin, destination, datetime(2026, 9, 19, 10, tzinfo=KST), "PUBLIC_TRANSPORT",
            )
        result = summarize_route(details).model_dump()
        self.assertEqual(result["line_name"], "2호선")
        self.assertNotIn("vehicle_number", result)
        self.assertIsNone(result["legs"][0]["vehicle_number"])


class PublicTransitGuidanceTests(unittest.TestCase):
    def test_backend_sse_keeps_bus_guidance_and_car_has_none(self):
        def handle(req):
            p = req.url.params
            origin = type("Point", (), {"latitude": float(p["start_y"]), "longitude": float(p["start_x"])})()
            destination = type("Point", (), {"latitude": float(p["end_y"]), "longitude": float(p["end_x"])})()
            return httpx.Response(200, json=transit_payload(origin, destination))

        app = create_app(settings=Settings(api_token="test-only", openai_api_key="test", kakao_rest_api_key="test"), transport=httpx.MockTransport(handle))
        with TestClient(app) as client:
            app.state.places, app.state.planner = PlaceClient(), Planner()
            response = client.post("/api/ai/v1/itinerary-jobs/stream", json=http_request(), headers={"Authorization": "Bearer test-only"})
            original_route = app.state.routes.route

            async def with_verified_numbers(*args):
                details = await original_route(*args)
                for leg in details.legs:
                    if leg.mode == "BUS":
                        leg.start.station_number = "00123"
                        leg.end.station_number = "00456"
                return details

            # Fixture represents already-verified external stop data, not Kakao fields.
            app.state.routes.route = with_verified_numbers
            enriched_response = client.post("/api/ai/v1/itinerary-jobs/stream", json=http_request(), headers={"Authorization": "Bearer test-only"})
            schema = client.get("/openapi.json").json()["components"]["schemas"]["TransitStopSummary"]
            self.assertIn("station_number", schema["properties"])

        self.assertEqual(response.status_code, 200)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        result = next(event["result"] for event in events if "result" in event)
        for route in result["days"][0]["routes"]:
            self.assertEqual(route["transport_type"], "CAR")
            self.assertNotIn("legs", route)
            self.assertNotIn("vehicle_number", route)
        for route in result["days"][1]["routes"]:
            self.assertEqual(route["vehicle_number"], "141(심야)")
            self.assertEqual(route["legs"][0]["start"], {"name": "출발", "station_number": None})
            self.assertEqual(route["legs"][0]["end"], {"name": "도착", "station_number": None})
        self.assertEqual(sum("result" in event for event in events), 1)
        self.assertFalse(all_keys(events) & {"path", "stops", "instructions", "vehicles"})

        self.assertEqual(enriched_response.status_code, 200)
        enriched_events = [json.loads(line[6:]) for line in enriched_response.text.splitlines() if line.startswith("data: ")]
        enriched_result = next(event["result"] for event in enriched_events if "result" in event)
        for route in enriched_result["days"][1]["routes"]:
            self.assertEqual(route["legs"][0]["start"]["station_number"], "00123")
            self.assertEqual(route["legs"][0]["end"]["station_number"], "00456")
