"""등록된 관광지 운영시간을 장소명·주소로 대조하고 기존 식당 시간과 함께 적용한다."""
from datetime import datetime
from functools import lru_cache
import json
from pathlib import Path
import unicodedata

from ai_service.core.errors import ServiceUnavailable
from ai_service.schemas.travel import Place
from ai_service.services.travel.opening_hours import OpeningHours


DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "travel" / "place_hours.json"


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("제주특별자치도", "제주").replace("제주도", "제주")
    return "".join(value.split()).casefold()


@lru_cache(maxsize=1)
def _known_hours():
    try:
        records = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return [(record, OpeningHours(record)) for record in records]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ServiceUnavailable(reason="place_hours_data_unavailable") from exc


def hours_for(place: Place) -> OpeningHours | None:
    if place._opening_hours is not None:
        return place._opening_hours
    if place.category == "식당":
        return place._restaurant_hours
    if place.category != "관광":
        return None
    for record, hours in _known_hours():
        if (_normalized(place.place_name) == _normalized(record["place_name"])
                and place.category == record["category"]
                and {_normalized(place.address), _normalized(place.road_address)}
                & {_normalized(address) for address in record["addresses"]}):
            place._opening_hours = hours
            return hours
    return None


def closed_error(place: Place) -> str:
    prefix = "restaurant_closed" if place.category == "식당" else "place_closed"
    return f"{prefix}:{place.provider_place_id}"


def fit_visit(place: Place, arrival: datetime, stay_minutes: int, latest: datetime) -> datetime:
    hours = hours_for(place)
    if hours is None:
        return arrival
    start = hours.next_start(arrival, stay_minutes, latest)
    if start is None:
        raise ValueError(closed_error(place))
    return start


def validate_visit(place: Place, start: datetime, end: datetime) -> bool:
    hours = hours_for(place)
    return hours is None or hours.contains(start, end)


def hours_context(place: Place) -> dict:
    """생성 모델에만 전달한다. 공개 응답 모델에는 필드를 추가하지 않는다."""
    hours = hours_for(place)
    if hours is None:
        return {}
    def periods(values):
        return [[f"{start // 60:02d}:{start % 60:02d}", f"{end // 60:02d}:{end % 60:02d}"]
                for start, end in values]
    return {"opening_hours": {
        "weekly": {day: periods(values) for day, values in hours.weekly.items()},
        "exceptions": {day.isoformat(): periods(values) for day, values in hours.exceptions.items()},
    }}
