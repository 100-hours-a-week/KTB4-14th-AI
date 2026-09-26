import json
import unittest

import httpx

from ai_service.config import Settings
from ai_service.errors import MusicRecommendationFailed
from fastapi.testclient import TestClient
from ai_service.main import create_app
from ai_service.pipeline import recommend_accommodations
from ai_service.routing import KakaoRoutes
from ai_service.schemas import GenerationResult, ItineraryStreamRequest, MusicRecommendation
from ai_service.streaming import stream_generation
from test_day_transport import places, request, selection, transit_payload, http_request


FORBIDDEN = {
    "path", "instructions", "stops", "vehicles", "bus_number", "station_id",
    "is_required", "stay_minutes", "travel_minutes_from_previous", "search_center",
    "timezone", "model_version", "warnings", "music_id", "routes", "origin", "destination",
    "is_estimated", "map_url", "from_day_number", "to_day_number", "from_sequence", "to_sequence",
    "from_provider_place_id", "to_provider_place_id", "progress", "request_id", "date", "http_status",
}


def all_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(all_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(all_keys(v) for v in value))
    return set()


class PlaceClient:
    def __init__(self, hotels=True):
        self.pool = places()
        self.hotels = hotels
        self.centers = []

    async def collect(self, body, include_accommodation=True):
        return [p for p in self.pool if include_accommodation or p.category != "숙소"]

    async def accommodations(self, body, latitude, longitude):
        self.centers.append((latitude, longitude))
        return [p for p in self.pool if p.category == "숙소"] if self.hotels else []


class Planner:
    settings = Settings()

    async def generate(self, context, feedback, places_only=False):
        result = selection()
        if places_only:
            result.days[0].items = result.days[0].items[:2]
        return result

    async def recommend_music(self, body):
        return MusicRecommendation(title="테스트 음악", artist="테스트",
                                   youtube_url="https://www.youtube.com/watch?v=abcdefghijk")


class CompactRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider_point_count = 0

        def handle(req):
            p = req.url.params
            origin = type("Point", (), {"latitude": float(p["start_y"]), "longitude": float(p["start_x"])})()
            destination = type("Point", (), {"latitude": float(p["end_y"]), "longitude": float(p["end_x"])})()
            payload = transit_payload(origin, destination)
            for route in payload["routes"]:
                path = route["steps"][0]["path"]
                start, end = path["points"]
                path["points"] = [[start[0] + (end[0] - start[0]) * i / 999,
                                   start[1] + (end[1] - start[1]) * i / 999] for i in range(1000)]
                self.provider_point_count += len(path["points"])
            return httpx.Response(200, json=payload)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.router = KakaoRoutes(self.http, Settings(kakao_rest_api_key="test-only"))
        self.places = PlaceClient()
        self.body = ItineraryStreamRequest.model_validate({
            **request().model_dump(mode="json"),
        })

    async def asyncTearDown(self):
        await self.http.aclose()

    async def events(self, client):
        return [json.loads(chunk.split("data: ", 1)[1])
                async for chunk in stream_generation(self.body, client, Planner(), Settings(), "req_test", self.router)
                if "data: " in chunk]

    async def test_accommodation_center_and_hotel_added(self):
        selected = selection()
        selected.days[0].items = selected.days[0].items[:2]
        pool = await self.places.collect(self.body, include_accommodation=False)
        combined, _, result = await recommend_accommodations(self.body, selected, pool, self.places)
        self.assertEqual(combined.days[0].items[-1].provider_place_id, "2")
        self.assertEqual(result.accommodations[0].place.category, "숙소")
        self.assertAlmostEqual(self.places.centers[0][0], (pool[0].latitude + pool[1].latitude) / 2)

    async def test_full_stream_completes_without_detailed_coordinates(self):
        events = await self.events(self.places)
        self.assertEqual(events[:-1], [
            {"stage": stage, "status": status}
            for stage in ("PLACES", "ACCOMMODATIONS", "ROUTES", "MUSIC")
            for status in ("STARTED", "COMPLETED")
        ])
        self.assertEqual(set(events[-1]), {"generation_job_id", "stage", "status", "data"})
        self.assertEqual(events[-1]["generation_job_id"], self.body.generation_job_id)
        self.assertEqual(sum("data" in event for event in events), 1)
        self.assertEqual([e["stage"] for e in events if e["status"] == "COMPLETED"],
                         ["PLACES", "ACCOMMODATIONS", "ROUTES", "MUSIC", "COMPLETE"])
        self.assertEqual(events[-1]["stage"], "COMPLETE")
        self.assertFalse(all_keys(events) & FORBIDDEN)
        self.assertGreater(self.provider_point_count, 1000)
        final = events[-1]["data"]
        self.assertNotIn("client_draft_id", all_keys(events))
        self.assertEqual(final["itinerary"]["preference"]["budget_type"], "KRW")
        self.assertNotIn("budget_currency", final["itinerary"]["preference"])
        self.assertEqual(set(final), {"itinerary", "music"})
        self.assertEqual(set(final["music"]), {"title", "artist", "youtube_url"})
        self.assertEqual(final["music"]["title"], "테스트 음악")
        first_day = final["itinerary"]["days"][0]
        self.assertEqual(first_day["items"][-1]["item_type"], "ACCOMMODATION")
        summary = first_day["items"][1]["route_from_previous"]
        self.assertEqual(set(summary), {"transport_type", "duration_minutes", "distance_meter", "total_fare_amount"})
        self.assertEqual(first_day["travel_date"], "2026-09-19")
        self.assertEqual(first_day["items"][0]["latitude"], self.places.pool[0].latitude)
        self.assertLess(len(json.dumps(summary)), 800)
        self.assertEqual(final["itinerary"]["days"][1]["items"][0]["route_from_previous"]["duration_minutes"], 10)

    async def test_missing_hotel_returns_422_at_accommodation_stage(self):
        with self.assertLogs("uvicorn.error.audigo", level="ERROR"):
            events = await self.events(PlaceClient(hotels=False))
        self.assertEqual(events[-1]["stage"], "ACCOMMODATIONS")
        self.assertEqual(events[-1]["status"], "FAILED")
        self.assertEqual(events[-1]["message"], "ai_itinerary_generation_failed")
        self.assertEqual(set(events[-1]["data"]), {"error_message"})
        self.assertTrue(all(set(event) == {"stage", "status"} for event in events[:-1]))
        self.assertFalse(any(e["stage"] in {"ROUTES", "COMPLETE"} for e in events))

    async def test_music_error_falls_back_instead_of_failing_trip(self):
        class FailingPlanner(Planner):
            async def recommend_music(self, body):
                raise MusicRecommendationFailed()

        with self.assertLogs("uvicorn.error.audigo", level="ERROR") as logs:
            events = [json.loads(chunk.split("data: ", 1)[1])
                      async for chunk in stream_generation(self.body, self.places, FailingPlanner(), Settings(), "req_test", self.router)
                      if "data: " in chunk]
        self.assertIn("music_fallback", "\n".join(logs.output))
        self.assertFalse(any(e["status"] == "FAILED" for e in events))
        final = events[-1]
        self.assertEqual(final["stage"], "COMPLETE")
        self.assertEqual(final["data"]["music"]["artist"], "조용필")
        self.assertFalse(all_keys(events) & FORBIDDEN)

    def test_http_stream_uses_same_compact_schema_and_header_trace_id(self):
        settings = Settings(api_token="test-only", openai_api_key="test", kakao_rest_api_key="test")
        app = create_app(settings=settings)
        with TestClient(app) as client:
            app.state.places = self.places
            app.state.planner = Planner()
            data = http_request(self.body)
            data["preference"].update(transport_type="CAR", extra_request=None)
            response = client.post("/api/ai/v1/itinerary-jobs/stream", json=data,
                                   headers={"Authorization": "Bearer test-only"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["x-request-id"].startswith("req_"))
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            self.assertEqual(events[-1]["stage"], "COMPLETE")
            self.assertFalse(all_keys(events) & (FORBIDDEN - {"routes", "from_sequence", "to_sequence"}))
            self.assertEqual(set(events[-2]["result"]), {"title", "days", "music"})
            self.assertEqual(len(events), 9)
            self.assertTrue(all(set(event) == {"stage", "status"} for event in events[:-2]))
            self.assertEqual(response.text.count("event: complete\n"), 1)
            self.assertEqual(response.text.count('"result":'), 1)
            self.assertEqual(response.text.count('"music":'), 1)
            self.assertNotIn("result", events[-1])
            result = events[-2]["result"]
            self.assertEqual(result["days"][0]["items"][-1]["place_type"], "ACCOMMODATION")
            self.assertNotIn("item_type", all_keys(result))
            for day in result["days"]:
                sequences = {item["sequence"] for item in day["items"]}
                self.assertEqual(len(day["routes"]), len(day["items"]) - 1)
                for route in day["routes"]:
                    self.assertIn(route["from_sequence"], sequences)
                    self.assertIn(route["to_sequence"], sequences)
                    self.assertLess(route["from_sequence"], route["to_sequence"])
            # Cross-day route is retained as a summary, never linked to the wrong day.
            self.assertIn("route_from_previous", result["days"][1]["items"][0])
            self.assertNotIn("route_from_previous", result["days"][0]["items"][1])

    def test_openapi_documents_backend_events_and_single_result(self):
        spec = create_app(settings=Settings()).openapi()
        response = spec["paths"]["/api/ai/v1/itinerary-jobs/stream"]["post"]["responses"]["200"]
        self.assertIn("ROUTE_OPTIMIZE_DONE", response["description"])
        content = response["content"]["text/event-stream"]
        self.assertEqual(content["schema"]["type"], "string")
        examples = content["examples"]
        names = {"started": "PLACE_RECOMMEND_STARTED", "done": "PLACE_RECOMMEND_DONE",
                 "result": "ROUTE_OPTIMIZE_DONE", "complete": "complete", "error": "error"}
        for name, event in names.items():
            wire = examples[name]["value"]
            self.assertIn(f"event: {event}\n", wire)
            self.assertTrue(wire.endswith("\n\n"))
            payload = json.loads(wire.split("data: ", 1)[1])
            if name in {"started", "done"}:
                self.assertEqual(set(payload), {"stage", "status"})
            elif name == "result":
                self.assertEqual(set(payload["result"]), {"title", "days", "music"})
            elif name == "complete":
                self.assertEqual(set(payload), {"travel_plan_id", "stage", "status"})
            else:
                self.assertEqual(set(payload), {"travel_plan_id", "stage", "status", "message", "data"})
                self.assertEqual(set(payload["data"]), {"error_message"})

    def test_openapi_has_only_compact_route_properties(self):
        schemas = create_app(settings=Settings()).openapi()["components"]["schemas"]
        self.assertNotIn("RouteDetails", schemas)
        self.assertNotIn("RouteLeg", schemas)
        self.assertFalse(set(schemas["RouteSummary"]["properties"]) & FORBIDDEN)
        self.assertEqual(set(schemas["RouteSummary"]["properties"]),
                         {"transport_type", "duration_minutes", "distance_meter", "total_fare_amount", "legs"})
        for name, schema in schemas.items():
            self.assertFalse(set(schema.get("properties", {})) & FORBIDDEN)
