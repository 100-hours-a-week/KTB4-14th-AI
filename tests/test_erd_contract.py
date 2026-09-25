"""Regression checks against AUDIGO_최종 (2).xlsx, without live API calls."""
import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_service.config import Settings
from ai_service.features import make_itinerary_response, schedule_selection, validate_itinerary
from ai_service.main import create_app
from ai_service.schemas import Duration, ItineraryRequest, ModelItinerary, ModelSelection, Preference
from test_day_transport import places, request, selection, http_request


class ErdContractTests(unittest.TestCase):
    def test_service_error_uses_existing_error_message_key(self):
        with TestClient(create_app(settings=Settings(api_token="test-only"))) as client:
            for endpoint in ("/internal/ai/itineraries/generate", "/api/ai/v1/itinerary-jobs/stream"):
                with self.subTest(endpoint=endpoint):
                    response = client.post(endpoint, json=http_request(),
                                           headers={"Authorization": "Bearer test-only"})
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(set(response.json()["data"]), {"error_message"})

    def test_local_korean_datetimes_need_no_timezone_suffix(self):
        naive = Duration(arrival_datetime="2026-09-19T10:00:00", departure_datetime="2026-09-20T18:00:00")
        utc = Duration(arrival_datetime="2026-09-19T01:00:00Z", departure_datetime="2026-09-20T09:00:00Z")
        self.assertEqual(naive.local_bounds(), utc.local_bounds())
        self.assertEqual(naive.model_dump(mode="json")["arrival_datetime"], "2026-09-19T10:00:00")

    def test_spreadsheet_currency_survives_generation_without_draft_id(self):
        data = request().model_dump(mode="json")
        data["preference"]["budget_type"] = "USD"
        body = ItineraryRequest.model_validate(data)
        generated = schedule_selection(body, selection(), places())
        result = make_itinerary_response(body, generated, validate_itinerary(body, generated, places()), "test")
        self.assertEqual(result.model_dump()["preference"]["budget_type"], "USD")
        self.assertNotIn("client_draft_id", result.model_dump())
        self.assertNotIn("budget_currency", result.model_dump()["preference"])

    def test_required_place_name_is_preserved(self):
        body = request()
        data = body.model_dump(mode="json")
        place = places()[0].model_dump(exclude={"source_category", "is_required"})
        data["required_places"] = [{**place, "order": 1}]
        body = ItineraryRequest.model_validate(data)
        generated = schedule_selection(body, selection(), places())
        result = make_itinerary_response(body, generated, validate_itinerary(body, generated, places()), "test").model_dump()
        self.assertEqual(result["required_places"][0]["place_name"], place["place_name"])
        self.assertNotIn("name", result["required_places"][0])

    def test_title_fits_erd_varchar_50(self):
        for cls, data in ((ModelSelection, selection().model_dump()),
                          (ModelItinerary, schedule_selection(request(), selection(), places()).model_dump())):
            with self.subTest(schema=cls.__name__):
                data["title"] = "가" * 50
                self.assertEqual(len(cls.model_validate(data).title), 50)
                data["title"] += "가"
                with self.assertRaises(ValidationError):
                    cls.model_validate(data)

    def test_pace_aliases_serialize_as_erd_enum(self):
        for source, expected in (("여유롭게", "RELAXED"), ("보통", "BALANCED"), ("TIGHT", "PACKED")):
            data = request().preference.model_dump()
            data.update(pace_type=source, transport_type="렌터카")
            normalized = Preference.model_validate(data).model_dump()
            self.assertEqual(normalized["pace_type"], expected)
            self.assertEqual(normalized["transport_type"], "CAR")

    def test_http_and_openapi_keep_existing_keys(self):
        settings = Settings(api_token="test-only-" * 4, openai_api_key="test", kakao_rest_api_key="test")
        app = create_app(settings=settings)
        pool = places()

        class Places:
            async def collect(self, body):
                return pool

        class Planner:
            async def generate(self, context, feedback):
                return selection()

        planner = Planner()
        planner.settings = settings
        with TestClient(app) as client:
            app.state.places = Places()
            app.state.planner = planner
            headers = {"Authorization": "Bearer " + settings.api_token}
            data = http_request(request(None, "CAR"))
            data.update(arrival_datetime="2026-09-19T10:00:00", departure_datetime="2026-09-20T18:00:00")
            response = client.post("/internal/ai/itineraries/generate", json=data, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["duration"], {key: data[key] for key in ("arrival_datetime", "departure_datetime")})
            self.assertEqual(response.json()["preference"]["budget_type"], "KRW")
            self.assertNotIn("client_draft_id", response.json())
            self.assertNotIn("budget_currency", response.text)
            for key in ("path", "stops", "instructions", "vehicles"):
                self.assertNotIn(f'"{key}":', response.text)
            route = response.json()["days"][0]["items"][1]["route_from_previous"]
            self.assertEqual(set(route), {"transport_type", "duration_minutes", "distance_meter", "total_fare_amount"})
            self.assertEqual(response.json()["days"][0]["travel_date"], "2026-09-19")
            spec = client.get("/openapi.json").json()["components"]["schemas"]
            self.assertIn("budget_type", spec["Preference"]["properties"])
            self.assertNotIn("budget_currency", spec["Preference"]["properties"])
            self.assertNotIn("client_draft_id", spec["ItineraryResponse"]["properties"])
            self.assertIn("place_name", spec["RequiredPlaceResponse"]["properties"])
            self.assertEqual(spec["ItineraryResponse"]["properties"]["title"]["maxLength"], 50)
            data["preference"]["budget_currency"] = data["preference"].pop("budget_type")
            self.assertEqual(client.post("/internal/ai/itineraries/generate", json=data, headers=headers).status_code, 400)


if __name__ == "__main__":
    unittest.main()
