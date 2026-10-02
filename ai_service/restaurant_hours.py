"""Manually verified restaurant hours, keyed by the existing Kakao place ID.

The Kakao Local API does not expose opening hours. The configured JSON file is
curated from a place's public listing; no undocumented map endpoint is called.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import unicodedata

from ai_service.errors import GenerationFailed, ServiceUnavailable
from ai_service.schemas import Place


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _minutes(value: str) -> int:
    if not isinstance(value, str) or not CLOCK.fullmatch(value):
        raise ValueError("hours must use HH:MM")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _intervals(value: object) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, list):
        raise ValueError("hours must be a list of [open, close] pairs")
    result = []
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("invalid opening period")
        start, end = _minutes(pair[0]), _minutes(pair[1])
        # Equal times mean 24 hours. A closing time before opening is next day.
        if end <= start:
            end += 1440
        result.append((start, end))
    return tuple(result)


class RestaurantHours:
    def __init__(self, record: dict):
        if not isinstance(record, dict) or not isinstance(record.get("weekly"), dict):
            raise ValueError("weekly hours are required")
        if not isinstance(record.get("source_url"), str) or not record["source_url"].startswith((
            "https://map.naver.com/", "https://place.naver.com/", "https://m.place.naver.com/",
            "https://pcmap.place.naver.com/"
        )):
            raise ValueError("a Naver Map source_url is required")
        weekly = record["weekly"]
        if set(weekly) != set(WEEKDAYS):
            raise ValueError("all seven weekdays are required")
        self.weekly = {day: _intervals(weekly[day]) for day in WEEKDAYS}
        raw_exceptions = record.get("exceptions", {})
        if not isinstance(raw_exceptions, dict):
            raise ValueError("exceptions must be an object")
        self.exceptions = {date.fromisoformat(key): _intervals(value)
                           for key, value in raw_exceptions.items()}
        self.place_name = record.get("place_name")
        self.address = record.get("address")

    def matches(self, place: Place) -> bool:
        def normalize(value: str) -> str:
            return "".join(unicodedata.normalize("NFC", value).split())

        if self.place_name and normalize(self.place_name) != normalize(place.place_name):
            return False
        if self.address and normalize(self.address) not in {
            normalize(place.address), normalize(place.road_address)
        }:
            return False
        return True

    def _periods(self, day: date):
        return self.exceptions.get(day, self.weekly[WEEKDAYS[day.weekday()]])

    def next_start(self, arrival: datetime, stay_minutes: int, latest: datetime) -> datetime | None:
        """Find a slot that contains the *whole* visit, including overnight hours."""
        for offset in (-1, 0, 1):
            day = arrival.date() + timedelta(days=offset)
            midnight = arrival.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset)
            for open_minute, close_minute in self._periods(day):
                start = max(arrival, midnight + timedelta(minutes=open_minute))
                close = midnight + timedelta(minutes=close_minute)
                if start + timedelta(minutes=stay_minutes) <= min(close, latest):
                    return start
        return None

    def contains(self, start: datetime, end: datetime) -> bool:
        minutes = int((end - start).total_seconds() / 60)
        return self.next_start(start, minutes, end) == start


def load_hours(path: Path) -> dict[str, RestaurantHours]:
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, dict):
            raise ValueError("root must be an object")
        return {str(place_id): RestaurantHours(value) for place_id, value in records.items()}
    except (OSError, ValueError, TypeError) as exc:
        raise ServiceUnavailable(reason="restaurant_hours_data_unavailable") from exc


def attach_verified_hours(
    places: list[Place], path: Path, known_hours: dict[str, RestaurantHours] | None = None,
) -> list[Place]:
    """Exclude unknown optional restaurants; never silently drop a required one."""
    hours = known_hours if known_hours is not None else load_hours(path)
    result = []
    for place in places:
        if place.category != "식당":
            result.append(place)
        elif place.provider_place_id in hours and hours[place.provider_place_id].matches(place):
            place._restaurant_hours = hours[place.provider_place_id]
            result.append(place)
        elif place.is_required:
            raise GenerationFailed(
                "필수 식당의 영업시간을 확인할 수 없습니다.",
                reason="required_restaurant_hours_missing",
                detail={"provider_place_id": place.provider_place_id},
            )
    return result


def fit_restaurant(place: Place, arrival: datetime, stay_minutes: int, latest: datetime) -> datetime:
    hours = place._restaurant_hours
    if hours is None:
        return arrival
    start = hours.next_start(arrival, stay_minutes, latest)
    if start is None:
        raise ValueError(f"restaurant_closed:{place.provider_place_id}")
    return start


def validate_restaurant_visit(place: Place, start: datetime, end: datetime) -> bool:
    hours = place._restaurant_hours
    return hours is None or hours.contains(start, end)
