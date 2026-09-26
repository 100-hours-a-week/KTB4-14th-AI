import json
import unittest

from ai_service.diagnostics import failure
from ai_service.errors import GenerationFailed
from ai_service.features import PACE_POLICIES, day_windows
from ai_service.pipeline import (
    lodging_days,
    places_day_minutes,
    trim_places_to_time,
    validate_places_selection,
)
from ai_service.schemas import ModelSelection
from test_day_transport import places, request


def short_last_day():
    # Day 2 ends at 11:30: only 150 minutes, too short for 3 RELAXED visits.
    body = request(None)
    body.duration.departure_datetime = body.duration.departure_datetime.replace(hour=11, minute=30)
    selection = ModelSelection(title="부산 여행", days=[
        {"date": "2026-09-19", "items": [{"provider_place_id": "0"}, {"provider_place_id": "1"}]},
        {"date": "2026-09-20", "items": [{"provider_place_id": i} for i in ("3", "4", "5")]},
    ])
    return body, selection


class TrimPlacesTests(unittest.TestCase):
    def test_overfull_short_day_is_trimmed_instead_of_failing(self):
        body, selection = short_last_day()
        pool = places()
        with self.assertRaisesRegex(Exception, "2026-09-20"):
            validate_places_selection(body, selection, pool)
        with self.assertLogs("uvicorn.error.audigo", level="INFO") as logs:
            trimmed = trim_places_to_time(body, selection, pool)
        validate_places_selection(body, trimmed, pool)
        window = day_windows(body)[1]
        by_id = {p.provider_place_id: p for p in pool}
        day = [by_id[i.provider_place_id] for i in trimmed.days[1].items]
        self.assertTrue(day)
        self.assertLessEqual(
            places_day_minutes(day, window, int(1 in lodging_days(body, pool)), PACE_POLICIES["RELAXED"]),
            window["available_minutes"],
        )
        self.assertEqual(trimmed.days[0], selection.days[0])  # Days that fit are untouched.
        self.assertIn("place_selection_trimmed", "\n".join(logs.output))

    def test_required_place_is_never_trimmed(self):
        body, selection = short_last_day()
        pool = places()
        pool[5].is_required = True
        trimmed = trim_places_to_time(body, selection, pool)
        self.assertIn("5", [i.provider_place_id for i in trimmed.days[1].items])


class DayWindowTests(unittest.TestCase):
    def test_schedule_window_is_wider_but_keeps_start_and_item_limits(self):
        body = request(None)
        for base, wide in zip(day_windows(body), day_windows(body, schedule=True)):
            self.assertEqual(wide["start"], base["start"])
            self.assertEqual((wide["min_items"], wide["max_items"]), (base["min_items"], base["max_items"]))
            self.assertGreaterEqual(wide["available_minutes"], base["available_minutes"])
        self.assertEqual(day_windows(body)[0]["end"], "21:00")
        self.assertEqual(day_windows(body, schedule=True)[0]["end"], "23:59")


class FailureLogTests(unittest.TestCase):
    def test_failure_log_has_reason_and_detail(self):
        exc = GenerationFailed("x", reason="accommodation_unavailable", detail={"day_number": 1, "candidates": 0})
        with self.assertLogs("uvicorn.error.audigo", level="ERROR") as logs:
            failure("pipeline_failed", exc, stage="ACCOMMODATIONS")
        entry = json.loads(logs.output[0].split(":", 2)[2])
        self.assertEqual(entry["reason"], "accommodation_unavailable")
        self.assertEqual(entry["detail"], {"day_number": 1, "candidates": 0})
        self.assertEqual(entry["stage"], "ACCOMMODATIONS")
