"""Reproducible HTTP stress check with local Kakao/OpenAI stand-ins.

Run: ./.venv/bin/python -m unittest discover -s tests -p test_http_request_stress.py -q
No provider request is sent. The FastAPI endpoint and itinerary pipeline are real.
"""

from collections import Counter
from copy import deepcopy
from datetime import date, timedelta
import logging
from pathlib import Path
import random
import unittest

import httpx
from fastapi.testclient import TestClient

from ai_service.api_examples import LEGACY_ITINERARY_REQUEST_EXAMPLE
from ai_service.config import Settings
from ai_service.main import create_app
from ai_service.pipeline import replace_closed_restaurant
from ai_service.restaurant_hours import attach_verified_hours
from ai_service.schemas import LegacyGenerationRequest, ModelSelection, MusicRecommendation, Place


PATH = "/internal/ai/itineraries/generate"
HOURS_FILE = Path(__file__).resolve().parents[1] / "ai_service/data/restaurant_hours.jeju.json"


def place(place_id, name, address, latitude, longitude, category):
    return Place(provider_place_id=place_id, place_name=name, address=address,
                 road_address=address, latitude=latitude, longitude=longitude,
                 category=category, source_category=category)


def jeju_pool():
    return [
        place("26338954", "성산일출봉", "제주특별자치도 서귀포시 성산읍 일출로 284-12",
              33.458056, 126.9425, "관광"),
        place("9733194", "네거리식당", "제주특별자치도 서귀포시 서문로29번길 20",
              33.2484915875436, 126.559290966856, "식당"),
        place("359631013", "풀풀", "제주특별자치도 서귀포시 표선면 중산간동로5220번길 45-1",
              33.3513608241343, 126.766279198123, "숙소"),
        place("11696879", "고성오일시장", "제주특별자치도 서귀포시 성산읍 고성오조로 93",
              33.45199979768546, 126.91329816476421, "관광"),
        place("1580890176", "고집돌우럭 중문점", "제주특별자치도 서귀포시 일주서로 879",
              33.2579811121134, 126.416704762779, "식당"),
        place("10289213", "두산봉", "제주특별자치도 서귀포시 성산읍 시흥리 2661",
              33.47711943892962, 126.88739174720739, "관광"),
    ]


class FakeJejuPlaces:
    def __init__(self):
        self.collect_calls = 0
        self.accommodation_calls = 0

    async def collect(self, body, include_accommodation=True):
        self.collect_calls += 1
        pool = jeju_pool()
        by_id = {p.provider_place_id: p for p in pool}
        for required in body.required_places:
            if required.provider_place_id in by_id:
                by_id[required.provider_place_id].is_required = True
            else:
                pool.append(Place(**required.model_dump(exclude={"order"}),
                                  source_category=required.category, is_required=True))
        pool = attach_verified_hours(pool, HOURS_FILE)
        return pool if include_accommodation else [p for p in pool if p.category != "숙소"]

    async def accommodations(self, body, latitude, longitude):
        self.accommodation_calls += 1
        return [jeju_pool()[2]]


class FakePlanner:
    settings = Settings(openai_model="stress-test")

    def __init__(self):
        self.place_calls = 0
        self.music_calls = 0

    async def generate(self, context, feedback, places_only=False):
        self.place_calls += 1
        dates = [window["date"] for window in context["day_windows"]]
        return ModelSelection(title="제주 일정 테스트", days=[
            {"date": dates[0], "items": [
                {"provider_place_id": "26338954"}, {"provider_place_id": "9733194"},
            ]},
            {"date": dates[1], "items": [
                {"provider_place_id": "1580890176"}, {"provider_place_id": "11696879"},
                {"provider_place_id": "10289213"},
            ]},
        ])

    async def recommend_music(self, body):
        self.music_calls += 1
        return MusicRecommendation(title="테스트 곡", artist="테스트 가수",
                                   youtube_url="https://www.youtube.com/watch?v=abcdefghijk")


def nested_request(rng, number):
    body = deepcopy(LEGACY_ITINERARY_REQUEST_EXAMPLE)
    start = date(2026, 10, 10) + timedelta(days=rng.randrange(60))
    body["generation_job_id"] = 10000 + number
    body["duration"] = {
        "arrival_datetime": f"{start.isoformat()}T{rng.randrange(8, 13):02d}:00:00",
        "departure_datetime": f"{(start + timedelta(days=1)).isoformat()}T{rng.randrange(18, 21):02d}:00:00",
    }
    body["headcount"] = rng.randrange(1, 5)
    body["companion_type"] = rng.choice(["COUPLE", "FRIENDS", "FAMILY"])
    body["preference"].update(
        pace_type=rng.choice(["RELAXED", "BALANCED", "PACKED"]),
        transport_type="CAR", distance_preference=rng.randrange(101),
        budget_min=rng.randrange(0, 500000, 50000),
        budget_max=rng.randrange(500000, 1100000, 50000),
        themes=rng.choice([["NATURE"], ["FOOD"], ["NATURE", "FOOD"]]),
        foods=rng.choice([[], ["KOREAN"], ["JAPANESE"]]),
        extra_request=rng.choice([None, "여유롭게 여행하고 싶어요."]),
    )
    if number % 2:
        body["required_places"] = []
    return body


def nested_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(nested_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(nested_keys(item) for item in value))
    return set()


class HttpRequestStressTests(unittest.TestCase):
    def test_1000_feasible_json_requests(self):
        """Send 1,000 varied, feasible JSON bodies through the real HTTP route."""
        seed = 20261003
        rng = random.Random(seed)
        settings = Settings(api_token="test-only", openai_api_key="test-only",
                            kakao_rest_api_key="test-only")
        external_calls = []

        def forbid_network(request):
            external_calls.append(str(request.url))
            raise AssertionError("unexpected external HTTP request")

        app = create_app(settings=settings, transport=httpx.MockTransport(forbid_network))
        counts = Counter()
        failures = []
        previous_log_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            with TestClient(app) as client:
                fake_places, fake_planner = FakeJejuPlaces(), FakePlanner()
                app.state.places, app.state.planner = fake_places, fake_planner
                for number in range(1000):
                    body = nested_request(rng, number)
                    response = client.post(PATH, json=body,
                                           headers={"Authorization": "Bearer test-only"})
                    counts[response.status_code] += 1
                    if response.status_code != 200:
                        failures.append((number, response.status_code, response.json()))
                    elif {"restaurant_hours", "weekly", "source_url", "music"} & nested_keys(response.json()):
                        failures.append((number, "unexpected response field"))
            print(f"feasible seed={seed} total=1000 status_counts={dict(counts)} "
                  f"stages={{'collect': {fake_places.collect_calls}, "
                  f"'accommodation': {fake_places.accommodation_calls}, "
                  f"'music': {fake_planner.music_calls}}} external_calls={len(external_calls)}")
            self.assertFalse(failures, failures[:10])
            self.assertEqual(counts, {200: 1000})
            self.assertEqual(fake_places.collect_calls, 1000)
            self.assertEqual(fake_places.accommodation_calls, 1000)
            self.assertEqual(fake_planner.music_calls, 1000)
            self.assertEqual(external_calls, [])
        finally:
            logging.disable(previous_log_level)

    def test_reorders_evening_restaurant_instead_of_rejecting_feasible_trip(self):
        rng = random.Random(20261002)
        for number in range(3):
            payload = nested_request(rng, number)
        body = LegacyGenerationRequest.model_validate(payload).generation_context()
        pool = jeju_pool()
        pool[0].is_required = True
        pool = [place for place in attach_verified_hours(pool, HOURS_FILE)
                if place.category != "숙소"]
        arrival, departure = body.duration.local_bounds()
        selected = ModelSelection(title="제주 일정 테스트", days=[
            {"date": arrival.date().isoformat(), "items": [
                {"provider_place_id": "26338954"}, {"provider_place_id": "9733194"},
            ]},
            {"date": departure.date().isoformat(), "items": [
                {"provider_place_id": "1580890176"}, {"provider_place_id": "11696879"},
                {"provider_place_id": "10289213"},
            ]},
        ])
        repaired = replace_closed_restaurant(body, selected, pool)
        self.assertEqual([item.provider_place_id for item in repaired.days[1].items],
                         ["11696879", "10289213", "1580890176"])

    def test_1000_json_requests(self):
        rng = random.Random(20261002)
        settings = Settings(api_token="test-only", openai_api_key="test-only",
                            kakao_rest_api_key="test-only")
        external_calls = []

        def forbid_network(request):
            external_calls.append(str(request.url))
            raise AssertionError("unexpected external HTTP request")

        app = create_app(settings=settings, transport=httpx.MockTransport(forbid_network))
        counts = Counter()
        failures = []
        request_ids = set()
        previous_log_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            with TestClient(app) as client:
                fake_places, fake_planner = FakeJejuPlaces(), FakePlanner()
                app.state.places, app.state.planner = fake_places, fake_planner
                route_calls = [0]
                original_route = app.state.routes.route

                async def count_route(*args, **kwargs):
                    route_calls[0] += 1
                    return await original_route(*args, **kwargs)

                app.state.routes.route = count_route
                for number in range(1000):
                    body = nested_request(rng, number)
                    if number < 800:
                        expected = 200
                        scenario = "feasible"
                    elif number < 850:
                        body.update(title="응답 필드", days=[])
                        expected, scenario = 400, "response_fields_in_request"
                    elif number < 900:
                        body.pop("duration")
                        expected, scenario = 400, "missing_duration"
                    elif number < 950:
                        day = body["duration"]["arrival_datetime"][:10]
                        body["duration"] = {
                            "arrival_datetime": f"{day}T23:00:00",
                            "departure_datetime": f"{day}T23:20:00",
                        }
                        expected, scenario = 422, "impossible_short_trip"
                    elif number < 975:
                        day = body["duration"]["arrival_datetime"][:10]
                        body["duration"] = {
                            "arrival_datetime": f"{day}T10:00:00",
                            "departure_datetime": f"{day}T12:00:00",
                        }
                        template = deepcopy(LEGACY_ITINERARY_REQUEST_EXAMPLE["required_places"][0])
                        body["required_places"] = [
                            {**template, "provider_place_id": f"required-{number}-{index}",
                             "order": index + 1}
                            for index in range(5)
                        ]
                        expected, scenario = 422, "too_many_required_places"
                    else:
                        body["required_places"] = [{
                            "provider": "KAKAO", "provider_place_id": "1148098112",
                            "place_name": "중문수두리보말칼국수",
                            "address": "제주특별자치도 서귀포시 중문동 2056-2",
                            "road_address": "제주특별자치도 서귀포시 천제연로 192",
                            "latitude": 33.25156773781329, "longitude": 126.42499942161753,
                            "category": "식당", "order": 1,
                        }]
                        expected, scenario = 422, "unverified_required_restaurant"

                    response = client.post(PATH, json=body,
                                           headers={"Authorization": "Bearer test-only"})
                    counts[(scenario, response.status_code)] += 1
                    request_id = response.headers.get("x-request-id")
                    if response.status_code != expected or not request_id:
                        failures.append((number, scenario, expected, response.status_code,
                                         response.json()))
                    if request_id in request_ids:
                        failures.append((number, "duplicate_request_id"))
                    request_ids.add(request_id)
                    if response.status_code == 200:
                        keys = nested_keys(response.json())
                        if {"restaurant_hours", "weekly", "source_url", "music"} & keys:
                            failures.append((number, "unexpected_response_fields", sorted(keys)))
                        if not response.json().get("days"):
                            failures.append((number, "missing_days"))
            print(f"seed=20261002 total=1000 status_counts={dict(Counter({status: sum(count for (scenario, code), count in counts.items() if code == status) for status in (200, 400, 422)}))} scenarios={dict(counts)} stages={{'collect': {fake_places.collect_calls}, 'accommodation': {fake_places.accommodation_calls}, 'routes': {route_calls[0]}, 'music': {fake_planner.music_calls}}} external_calls={len(external_calls)}")
            self.assertEqual(len(request_ids), 1000)
            self.assertEqual(external_calls, [])
            self.assertFalse(failures, failures[:10])
            self.assertEqual(counts[("feasible", 200)], 800)
            self.assertEqual(fake_planner.music_calls, 800)
            self.assertEqual(fake_places.accommodation_calls, 800)
            self.assertGreaterEqual(route_calls[0], 800)
        finally:
            logging.disable(previous_log_level)


if __name__ == "__main__":
    unittest.main()
