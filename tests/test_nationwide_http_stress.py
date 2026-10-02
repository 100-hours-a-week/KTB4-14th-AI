"""1,000 reproducible itinerary POSTs across 17 Korean regions.

The region names and coordinates are test fixtures. Provider calls are blocked;
this checks the API and scheduling pipeline, not nationwide live place coverage.
"""

from collections import Counter
from datetime import date, timedelta
import json
import logging
from pathlib import Path
import random
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient

from ai_service.config import Settings
from ai_service.main import create_app
from ai_service.schemas import ModelSelection, MusicRecommendation, Place


PATH = "/internal/ai/itineraries/generate"
REGIONS = (
    ("서울특별시 종로구", 37.57, 126.98),
    ("부산광역시 해운대구", 35.16, 129.16),
    ("대구광역시 중구", 35.87, 128.60),
    ("인천광역시 연수구", 37.40, 126.65),
    ("광주광역시 동구", 35.15, 126.85),
    ("대전광역시 유성구", 36.36, 127.38),
    ("울산광역시 남구", 35.54, 129.31),
    ("세종특별자치시", 36.48, 127.29),
    ("경기도 수원시", 37.26, 127.03),
    ("강원특별자치도 강릉시", 37.75, 128.90),
    ("충청북도 청주시", 36.64, 127.49),
    ("충청남도 천안시", 36.81, 127.15),
    ("전북특별자치도 전주시", 35.82, 127.14),
    ("전라남도 여수시", 34.76, 127.66),
    ("경상북도 경주시", 35.85, 129.22),
    ("경상남도 창원시", 35.23, 128.68),
    ("제주특별자치도 서귀포시", 33.25, 126.56),
)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def pool_for_region(region_index, region_name, latitude, longitude):
    categories = ("관광", "식당", "숙소", "관광", "식당", "관광")
    result = []
    for index, category in enumerate(categories):
        result.append(Place(
            provider_place_id=f"r{region_index}-{index}",
            place_name=f"테스트 장소 {index}",
            address=f"{region_name} 테스트 주소 {index}",
            road_address=f"{region_name} 테스트 주소 {index}",
            latitude=latitude + index * 0.001,
            longitude=longitude + index * 0.001,
            category=category,
            source_category=category,
        ))
    return result


def simulated_hours_records():
    records = {}
    for region_index, region in enumerate(REGIONS):
        for restaurant in (place for place in pool_for_region(region_index, *region)
                           if place.category == "식당"):
            records[restaurant.provider_place_id] = {
                "place_name": restaurant.place_name,
                "address": restaurant.address,
                "source_url": "https://map.naver.com/p/entry/place/test-only",
                "weekly": {day: [["08:00", "22:00"]] for day in WEEKDAYS},
            }
    return records


def mock_kakao(request, calls):
    if request.url.host != "dapi.kakao.com":
        raise AssertionError("unexpected external provider")
    calls.append(request.url.path)
    params = request.url.params
    if request.url.path.startswith("/v2/routing/"):
        start = [float(params["start_x"]), float(params["start_y"])]
        end = [float(params["end_x"]), float(params["end_y"])]
        if request.url.path.endswith("/walk"):
            return httpx.Response(200, json={
                "status": "OK", "route": {
                    "properties": {"totalTime": 300, "totalDistance": 250},
                    "legs": [{"steps": [{
                        "properties": {"guidance": "걷기", "distance": 250},
                        "path": {"points": [start, end]},
                    }]}],
                },
            })
        if request.url.path.endswith("/publictraffic"):
            return httpx.Response(200, json={
                "status": "OK", "routes": [{
                    "properties": {"totalTime": 600, "totalDistance": 1000},
                    "steps": [{
                        "properties": {
                            "type": "BUS", "time": 600, "distance": 1000,
                            "vehicles": [{"name": "100", "type": "일반"}],
                            "stops": [{"name": "출발"}, {"name": "도착"}],
                        },
                        "path": {"points": [start, end]},
                    }],
                }],
            })
        raise AssertionError("unexpected routing endpoint")
    if request.url.path.endswith("/address.json"):
        name = params["query"]
        region = next(row for row in REGIONS if row[0] == name)
        return httpx.Response(200, json={"documents": [{
            "address_type": "REGION", "address_name": name,
            "y": str(region[1]), "x": str(region[2]),
        }]})
    if not request.url.path.endswith(("/keyword.json", "/category.json")):
        raise AssertionError("unexpected Kakao endpoint")
    longitude, latitude = float(params["x"]), float(params["y"])
    region_index = min(range(len(REGIONS)), key=lambda index:
                       (latitude - REGIONS[index][1]) ** 2 +
                       (longitude - REGIONS[index][2]) ** 2)
    codes = {"관광": "AT4", "식당": "FD6", "숙소": "AD5"}
    group = params.get("category_group_code")
    documents = [{
        "id": place.provider_place_id, "place_name": place.place_name,
        "address_name": place.address, "road_address_name": place.road_address,
        "y": str(place.latitude), "x": str(place.longitude),
        "category_group_code": codes[place.category],
    } for place in pool_for_region(region_index, *REGIONS[region_index])
        if group is None or codes[place.category] == group]
    return httpx.Response(200, json={"documents": documents})


class NationwidePlanner:
    settings = Settings(openai_model="nationwide-stress-test")

    def __init__(self):
        self.music_calls = 0

    async def generate(self, context, feedback, places_only=False):
        dates = [window["date"] for window in context["day_windows"]]
        region_index = context["request"]["region"]["region_id"] - 1
        return ModelSelection(title="전국 일정 테스트", days=[
            {"date": dates[0], "items": [
                {"provider_place_id": f"r{region_index}-0"},
                {"provider_place_id": f"r{region_index}-1"},
            ]},
            {"date": dates[1], "items": [
                {"provider_place_id": f"r{region_index}-3"},
                {"provider_place_id": f"r{region_index}-4"},
                {"provider_place_id": f"r{region_index}-5"},
            ]},
        ])

    async def recommend_music(self, body):
        self.music_calls += 1
        return MusicRecommendation(title="테스트 곡", artist="테스트 가수",
                                   youtube_url="https://www.youtube.com/watch?v=abcdefghijk")


def request_for_region(rng, number):
    region_index = number % len(REGIONS)
    region_name, latitude, longitude = REGIONS[region_index]
    start = date(2026, 10, 10) + timedelta(days=rng.randrange(60))
    body = {
        "generation_job_id": 20000 + number,
        "region": {"region_id": region_index + 1, "full_name": region_name},
        "duration": {
            "arrival_datetime": f"{start.isoformat()}T{rng.randrange(8, 13):02d}:00:00",
            "departure_datetime": f"{(start + timedelta(days=1)).isoformat()}T{rng.randrange(18, 21):02d}:00:00",
        },
        "headcount": rng.randrange(1, 5),
        "companion_type": rng.choice(["COUPLE", "FRIENDS", "FAMILY"]),
        "preference": {
            "pace_type": rng.choice(["RELAXED", "BALANCED", "PACKED"]),
            "transport_type": rng.choice(["CAR", "WALK", "PUBLIC_TRANSPORT"]),
            "budget_min": rng.randrange(0, 500000, 50000),
            "budget_max": rng.randrange(500000, 1100000, 50000),
            "budget_type": "KRW",
            "distance_preference": rng.randrange(101),
            "themes": rng.choice([["NATURE"], ["FOOD"], ["NATURE", "FOOD"]]),
            "foods": rng.choice([[], ["KOREAN"], ["JAPANESE"]]),
            "extra_request": rng.choice([None, "여유롭게 여행하고 싶어요."]),
        },
        "required_places": [],
    }
    if number % 2 == 0:
        body["required_places"] = [{
            "provider": "KAKAO", "provider_place_id": f"r{region_index}-0",
            "place_name": "테스트 장소 0",
            "address": f"{region_name} 테스트 주소 0",
            "road_address": f"{region_name} 테스트 주소 0",
            "latitude": latitude, "longitude": longitude,
            "category": "관광", "order": 1,
        }]
    return body


def nested_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(nested_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(nested_keys(item) for item in value))
    return set()


class NationwideHttpStressTests(unittest.TestCase):
    def test_1000_feasible_requests_across_17_regions(self):
        rng = random.Random(20261004)
        status_counts, region_counts = Counter(), Counter()
        failures = []
        mock_calls = []
        previous_log_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            with tempfile.TemporaryDirectory() as directory:
                hours_file = Path(directory) / "nationwide_hours.json"
                hours_file.write_text(json.dumps(simulated_hours_records(), ensure_ascii=False),
                                      encoding="utf-8")
                settings = Settings(api_token="test-only", openai_api_key="test-only",
                                    kakao_rest_api_key="test-only",
                                    restaurant_hours_file=hours_file)
                app = create_app(settings=settings, transport=httpx.MockTransport(
                    lambda request: mock_kakao(request, mock_calls)))
                with TestClient(app) as client:
                    planner = NationwidePlanner()
                    app.state.planner = planner
                    for number in range(1000):
                        body = request_for_region(rng, number)
                        region_counts[body["region"]["full_name"]] += 1
                        response = client.post(PATH, json=body,
                                               headers={"Authorization": "Bearer test-only"})
                        status_counts[response.status_code] += 1
                        if response.status_code != 200:
                            failures.append((number, body["region"]["full_name"],
                                             response.status_code, response.json()))
                        elif {"restaurant_hours", "weekly", "source_url", "music"} & nested_keys(response.json()):
                            failures.append((number, "unexpected response field"))
            print(f"nationwide seed=20261004 total=1000 regions={len(region_counts)} "
                  f"min_per_region={min(region_counts.values())} "
                  f"status_counts={dict(status_counts)} "
                  f"music={planner.music_calls} mocked_kakao_calls={len(mock_calls)} "
                  "external_network_calls=0")
            self.assertFalse(failures, failures[:10])
            self.assertEqual(len(region_counts), 17)
            self.assertEqual(status_counts, {200: 1000})
            self.assertEqual(planner.music_calls, 1000)
            self.assertGreaterEqual(len(mock_calls), 1000)
        finally:
            logging.disable(previous_log_level)


if __name__ == "__main__":
    unittest.main()
