"""기존 카카오 장소 ID를 기준으로 직접 확인한 식당 영업시간을 관리한다.

카카오 장소 검색 API는 영업시간을 제공하지 않는다. 설정한 JSON 파일에는
공개된 장소 정보에서 확인한 시간을 기록하며, 비공개 지도 API는 호출하지 않는다.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from ai_service.core.errors import GenerationFailed, ServiceUnavailable
from ai_service.services.travel.opening_hours import OpeningHours
from ai_service.schemas.travel import (
    Place
)


class RestaurantHours(OpeningHours):
    """기존 네이버 지도 출처 식당 데이터의 검증 정책을 유지한다."""

    def __init__(self, record: dict):
        if not isinstance(record, dict) or not isinstance(record.get("source_url"), str) or not record["source_url"].startswith((
            "https://map.naver.com/", "https://place.naver.com/", "https://m.place.naver.com/",
            "https://pcmap.place.naver.com/"
        )):
            raise ValueError("a Naver Map source_url is required")
        super().__init__(record)


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
