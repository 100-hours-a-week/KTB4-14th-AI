"""Deterministic schedule stress cases without OpenAI or Kakao network calls."""

from collections import Counter
from datetime import datetime, timedelta
import os
import random
import unittest
from unittest.mock import AsyncMock

from ai_service.errors import GenerationFailed, InvalidModelOutput
from ai_service.features import (
    complete_selection,
    schedule_selection,
    validate_generation_window,
    validate_itinerary,
)
from ai_service.pipeline import (
    fallback_short_single_day,
    fallback_multi_day,
    recommend_accommodations,
    recommend_places,
    trim_places_to_time,
    validate_places_selection,
)
from ai_service.schemas import KST, ModelSelection, RequiredPlace
from test_day_transport import places, request


def varied_places(rng):
    pool = places()
    for place in pool:
        place.latitude += rng.uniform(-0.2, 0.2)
        place.longitude += rng.uniform(-0.2, 0.2)
    return pool


def choose_days(rng, pool, dates):
    visits = [place for place in pool if place.category != "숙소"]
    selected = rng.sample(visits, rng.randrange(len(visits) + 1))
    cutoff = rng.randrange(len(selected) + 1) if len(dates) > 1 else len(selected)
    groups = [selected[:cutoff], selected[cutoff:]] if len(dates) > 1 else [selected]
    return ModelSelection(title="무작위 일정", days=[
        {"date": date, "items": [{"provider_place_id": place.provider_place_id}
                                for place in group]}
        for date, group in zip(dates, groups)
    ]), selected


class ScheduleFuzzTests(unittest.IsolatedAsyncioTestCase):
    def test_700_random_same_day_schedules_have_no_late_validation_failure(self):
        rng = random.Random(2801)
        counts = Counter()
        retry_reasons = Counter()
        for case in range(700):
            body = request(None, rng.choice(["CAR", "WALK", "PUBLIC_TRANSPORT"]))
            body.preference.pace_type = rng.choice(["RELAXED", "BALANCED", "PACKED"])
            body.headcount = rng.choice([1, 2, 5, 10, 20])
            arrival = datetime(2026, 9, 19, rng.randrange(7, 21),
                               rng.choice([0, 15, 30, 45]), tzinfo=KST)
            body.duration.arrival_datetime = arrival
            body.duration.departure_datetime = min(
                arrival + timedelta(minutes=rng.randrange(30, 780, 15)),
                datetime(2026, 9, 19, 23, 59, tzinfo=KST),
            )
            pool = [place for place in varied_places(rng) if place.category != "숙소"]
            selected, _ = choose_days(rng, pool, ["2026-09-19"])
            with self.subTest(case=case, pace=body.preference.pace_type,
                              arrival=arrival.isoformat()):
                try:
                    validate_generation_window(body)
                except GenerationFailed as exc:
                    self.assertEqual(exc.reason, "insufficient_trip_time")
                    counts["impossible_window"] += 1
                    continue
                try:
                    completed = complete_selection(body, selected, pool, places_only=True)
                    trimmed = trim_places_to_time(body, completed, pool)
                    validate_places_selection(body, trimmed, pool)
                except InvalidModelOutput as exc:
                    retry_reasons[str(exc).split(":", 1)[-1].strip()] += 1
                    fallback = fallback_short_single_day(body, pool)
                    if fallback is not None:
                        validate_itinerary(body, schedule_selection(body, fallback, pool), pool)
                        counts["fallback_completed"] += 1
                    else:
                        counts["selection_needs_retry"] += 1
                    continue
                days = validate_itinerary(
                    body, schedule_selection(body, trimmed, pool), pool
                )
                self.assertTrue(any(day.items for day in days))
                counts["completed"] += 1
        self.assertEqual(sum(counts.values()), 700)
        self.assertGreaterEqual(counts["completed"] + counts["fallback_completed"], 610)
        self.assertEqual(counts["selection_needs_retry"], 0)
        if os.environ.get("AUDIGO_FUZZ_REPORT") == "1":
            print(f"same_day seed=2801 cases=700 {dict(counts)} reasons={retry_reasons.most_common(5)}")

    async def test_300_random_overnight_schedules_preserve_required_visits(self):
        rng = random.Random(2810)
        counts = Counter()
        retry_reasons = Counter()
        hotel_reasons = Counter()
        failed_cases = []
        fallback_used = 0
        integrated_fallback_checked = False
        for case in range(300):
            body = request(None, rng.choice(["CAR", "WALK", "PUBLIC_TRANSPORT"]))
            body.preference.pace_type = rng.choice(["RELAXED", "BALANCED", "PACKED"])
            body.headcount = rng.choice([1, 2, 5, 10])
            body.duration.arrival_datetime = datetime(
                2026, 9, 19, rng.randrange(7, 19), rng.choice([0, 30]), tzinfo=KST,
            )
            body.duration.departure_datetime = datetime(
                2026, 9, 20, rng.randrange(9, 19), rng.choice([0, 30]), tzinfo=KST,
            )
            pool = varied_places(rng)
            selected, visits = choose_days(rng, pool, ["2026-09-19", "2026-09-20"])
            if visits and rng.random() < 0.4:
                required = visits[0]
                required.is_required = True
                body.required_places.append(RequiredPlace(
                    **required.model_dump(exclude={"is_required", "source_category"}),
                    order=1,
                ))
            # 15개 숙소 후보를 실제 검색 결과처럼 서로 다른 위치에 둔다.
            anchor = visits[0] if visits else pool[0]
            hotels = [pool[2].model_copy(update={
                "provider_place_id": f"hotel-{number}",
                "latitude": anchor.latitude + rng.uniform(-0.12, 0.12),
                "longitude": anchor.longitude + rng.uniform(-0.12, 0.12),
            }) for number in range(15)]
            client = type("FakePlaces", (), {})()
            client.accommodations = AsyncMock(return_value=hotels)
            with self.subTest(case=case, pace=body.preference.pace_type,
                              required=len(body.required_places)):
                try:
                    validate_generation_window(body)
                except GenerationFailed as exc:
                    self.assertEqual(exc.reason, "insufficient_trip_time")
                    counts["impossible_window"] += 1
                    continue
                try:
                    completed = complete_selection(body, selected, pool, places_only=True)
                    trimmed = trim_places_to_time(body, completed, pool)
                    validate_places_selection(body, trimmed, pool)
                except InvalidModelOutput as exc:
                    retry_reasons[str(exc).split(":", 1)[-1].strip()] += 1
                    trimmed = fallback_multi_day(body, pool, selected)
                    if trimmed is None:
                        counts["selection_needs_retry"] += 1
                        failed_cases.append((case, "selection"))
                        continue
                    fallback_used += 1
                    if not integrated_fallback_checked:
                        planner = type("FakePlanner", (), {})()
                        planner.generate = AsyncMock(return_value=selected)
                        integrated = await recommend_places(body, pool, planner)
                        validate_places_selection(body, integrated, pool)
                        self.assertEqual(planner.generate.await_count, 2)
                        integrated_fallback_checked = True
                try:
                    combined, complete_pool = await recommend_accommodations(
                        body, trimmed, pool, client
                    )
                except GenerationFailed as exc:
                    self.assertIn(exc.reason, {
                        "accommodation_unavailable", "accommodation_schedule_invalid",
                        "accommodation_anchor_missing",
                    })
                    counts["hotel_unavailable"] += 1
                    failed_cases.append((case, "hotel"))
                    hotel_reasons.update(exc.detail.get("rejections", {}))
                    continue
                days = validate_itinerary(
                    body, schedule_selection(body, combined, complete_pool),
                    complete_pool,
                )
                actual_ids = {item.provider_place_id for day in days for item in day.items}
                self.assertTrue({p.provider_place_id for p in body.required_places} <= actual_ids)
                self.assertEqual(days[0].items[-1].item_type, "ACCOMMODATION")
                counts["completed"] += 1
        self.assertEqual(sum(counts.values()), 300)
        self.assertGreaterEqual(counts["completed"], 299)
        self.assertEqual(counts["hotel_unavailable"], 0)
        self.assertEqual(counts["selection_needs_retry"], 0)
        self.assertTrue(integrated_fallback_checked)
        if os.environ.get("AUDIGO_FUZZ_REPORT") == "1":
            print(f"overnight seed=2810 cases=300 {dict(counts)} fallback_used={fallback_used} remaining={failed_cases} reasons={retry_reasons.most_common(5)} hotel_reasons={hotel_reasons.most_common(3)}")
