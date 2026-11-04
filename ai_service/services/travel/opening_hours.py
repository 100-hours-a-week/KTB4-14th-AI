"""관광지·식당이 공유하는 요일별 운영시간과 전체 방문 구간 검증."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import re
import unicodedata

from ai_service.schemas.travel import Place


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


class OpeningHours:
    def __init__(self, record: dict):
        if not isinstance(record, dict) or not isinstance(record.get("weekly"), dict):
            raise ValueError("weekly hours are required")
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


