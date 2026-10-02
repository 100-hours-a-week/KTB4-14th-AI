import unittest

import httpx
from fastapi.testclient import TestClient

from ai_service.config import Settings
from ai_service.errors import GenerationFailed, MusicRecommendationFailed
from ai_service.main import create_app
from ai_service.pipeline import generate_plan, recommend_accommodations
from ai_service.routing import KakaoRoutes
from ai_service.schemas import MusicRecommendation
from test_day_transport import places, request, selection, transit_payload, http_request


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
                route["properties"]["fare"] = {"value": 1650}
                path = route["steps"][0]["path"]
                start, end = path["points"]
                path["points"] = [[start[0] + (end[0] - start[0]) * i / 999,
                                   start[1] + (end[1] - start[1]) * i / 999] for i in range(1000)]
                self.provider_point_count += len(path["points"])
            return httpx.Response(200, json=payload)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.router = KakaoRoutes(self.http, Settings(kakao_rest_api_key="test-only"))
        self.places = PlaceClient()
        self.body = request()

    async def asyncTearDown(self):
        await self.http.aclose()

    async def test_accommodation_center_and_hotel_added(self):
        selected = selection()
        selected.days[0].items = selected.days[0].items[:2]
        pool = await self.places.collect(self.body, include_accommodation=False)
        combined, hotel_pool = await recommend_accommodations(self.body, selected, pool, self.places)
        self.assertEqual(combined.days[0].items[-1].provider_place_id, "2")
        self.assertEqual(next(p for p in hotel_pool if p.provider_place_id == "2").category, "숙소")
        self.assertAlmostEqual(self.places.centers[0][0], (pool[0].latitude + pool[1].latitude) / 2)

    async def test_pipeline_returns_compact_itinerary_after_music(self):
        result = await generate_plan(self.body, self.places, Planner(), self.router)
        final = result.model_dump(mode="json")
        self.assertEqual(set(final), {"itinerary", "music"})
        self.assertEqual(final["music"]["title"], "테스트 음악")
        self.assertEqual(final["itinerary"]["preference"]["budget_type"], "KRW")
        self.assertNotIn("client_draft_id", all_keys(final))
        self.assertNotIn("path", all_keys(final))
        self.assertGreater(self.provider_point_count, 1000)
        first_day = final["itinerary"]["days"][0]
        self.assertEqual(first_day["items"][-1]["item_type"], "ACCOMMODATION")
        summary = first_day["items"][1]["route_from_previous"]
        self.assertEqual(set(summary), {"transport_type", "duration_minutes", "distance_meter", "line_name", "vehicle_number", "total_fare_amount", "legs"})
        self.assertEqual(final["itinerary"]["days"][1]["items"][0]["route_from_previous"]["duration_minutes"], 10)

    async def test_missing_hotel_raises_generation_failed(self):
        with self.assertRaises(GenerationFailed), self.assertLogs("uvicorn.error.audigo", level="ERROR"):
            await generate_plan(self.body, PlaceClient(hotels=False), Planner(), self.router)

    async def test_music_error_falls_back(self):
        class FailingPlanner(Planner):
            async def recommend_music(self, body):
                raise MusicRecommendationFailed()

        with self.assertLogs("uvicorn.error.audigo", level="ERROR") as logs:
            result = await generate_plan(self.body, self.places, FailingPlanner(), self.router)
        self.assertIn("music_fallback", "\n".join(logs.output))
        self.assertEqual(result.music.artist, "조용필")

    def test_http_generate_keeps_json_contract(self):
        settings = Settings(api_token="test-only", openai_api_key="test", kakao_rest_api_key="test")
        app = create_app(settings=settings)
        with TestClient(app) as client:
            app.state.places = self.places
            app.state.planner = Planner()
            data = http_request(self.body)
            data["preference"].update(transport_type="CAR", extra_request=None)
            response = client.post("/internal/ai/itineraries/generate", json=data,
                                   headers={"Authorization": "Bearer test-only"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertTrue(response.headers["x-request-id"].startswith("req_"))
        self.assertNotIn("music", response.json())
        route = response.json()["days"][0]["items"][1]["route_from_previous"]
        self.assertEqual(set(route), {"transport_type", "duration_minutes", "distance_meter",
                                     "line_name", "vehicle_number", "total_fare_amount", "legs"})
        self.assertEqual(route["legs"], [])
        self.assertIsNone(route["line_name"])
        self.assertIsNone(route["vehicle_number"])
        self.assertEqual(response.json()["days"][0]["items"][-1]["item_type"], "ACCOMMODATION")

    def test_http_transit_returns_total_fare_and_compact_stops(self):
        settings = Settings(api_token="test-only", openai_api_key="test", kakao_rest_api_key="test")
        app = create_app(settings=settings)
        with TestClient(app) as client:
            app.state.places = self.places
            app.state.planner = Planner()
            app.state.routes = self.router
            response = client.post("/internal/ai/itineraries/generate", json=http_request(self.body),
                                   headers={"Authorization": "Bearer test-only"})
        self.assertEqual(response.status_code, 200, response.text)
        route = response.json()["days"][1]["items"][0]["route_from_previous"]
        self.assertEqual(route["total_fare_amount"], 1650)
        self.assertEqual(route["vehicle_number"], "141(심야)")
        leg = route["legs"][0]
        self.assertEqual(set(leg), {"mode", "line_name", "vehicle_number", "start", "end"})
        self.assertEqual(set(leg["start"]), {"name", "station_number"})
        self.assertFalse(all_keys(route) & {"path", "boarding_stop", "alighting_stop", "duration_minute"})

    def test_openapi_has_only_compact_route_properties(self):
        spec = create_app(settings=Settings()).openapi()
        self.assertNotIn("/api/ai/v1/itinerary-jobs/stream", spec["paths"])
        schemas = spec["components"]["schemas"]
        self.assertNotIn("RouteDetails", schemas)
        self.assertEqual(set(schemas["RouteSummary"]["properties"]),
                         {"transport_type", "duration_minutes", "distance_meter", "line_name", "vehicle_number", "total_fare_amount", "legs"})

        self.assertEqual(set(schemas["TransitLegSummary"]["properties"]),
                         {"mode", "line_name", "vehicle_number", "start", "end"})
        self.assertEqual(set(schemas["TransitStopSummary"]["properties"]),
                         {"name", "station_number"})
