from __future__ import annotations

from datetime import datetime, time, timedelta
import json
import logging
import math

from ai_service.errors import GenerationFailed, InvalidModelOutput
from ai_service.model import OpenAIPlanner
from ai_service.places import KakaoPlaces, distance_km, travel_minutes
from ai_service.schemas import (
    ItineraryDay,
    ItineraryItem,
    ItineraryRequest,
    ItineraryResponse,
    ModelItinerary,
    ModelSelection,
    PACE_ALIASES,
    Place,
)


logger = logging.getLogger(__name__)
PACE_POLICIES = {
    "RELAXED": {
        "min_items": 3,
        "max_items": 5,
        "관광": [90, 150],
        "식당": [60, 90],
        "숙소": [60, 90],
    },
    "BALANCED": {
        "min_items": 4,
        "max_items": 6,
        "관광": [60, 120],
        "식당": [45, 75],
        "숙소": [45, 75],
    },
    "PACKED": {
        "min_items": 6,
        "max_items": 8,
        "관광": [40, 75],
        "식당": [40, 60],
        "숙소": [30, 60],
    },
}


def day_windows(request: ItineraryRequest) -> list[dict]:
    arrival, departure = request.duration.local_bounds()
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    windows = []
    for index in range((departure.date() - arrival.date()).days + 1):
        day = arrival.date() + timedelta(days=index)
        start = max(datetime.combine(day, time(9)), arrival)
        end = min(datetime.combine(day, time(21)), departure)
        if day == arrival.date() and arrival.time() >= time(21):
            end = min(datetime.combine(day, time(23, 59)), departure)
        start = min(start, end)
        # Public schedules have minute precision. Never round arrival down into
        # unavailable time (e.g. 13:00:30 must first allow a 13:01 visit).
        if start.second or start.microsecond:
            start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = end.replace(second=0, microsecond=0)
        start = min(start, end)
        minutes = max(0, int((end - start).total_seconds() / 60))
        # Short boundary dates may be transit-only. They remain present in days[].
        min_stay = min(policy[category][0] for category in ("관광", "식당", "숙소"))
        maximum = min(policy["max_items"], max(0, minutes // min_stay))
        minimum = min(maximum, math.ceil(policy["min_items"] * minutes / 720))
        windows.append(
            {
                "date": day.isoformat(),
                "start": start.strftime("%H:%M"),
                "end": end.strftime("%H:%M"),
                "available_minutes": minutes,
                "min_items": minimum,
                "max_items": maximum,
                "needs_accommodation": day < departure.date()
                and minutes >= policy["숙소"][0],
                "needs_tour_and_restaurant": minutes >= 240,
            }
        )
    return windows


def select_candidates(request: ItineraryRequest, places: list[Place]) -> list[Place]:
    days = len(day_windows(request))
    required = [p for p in places if p.is_required]
    result = required.copy()
    limits = {
        "관광": max(8, days * 4),
        "식당": max(6, days * 3),
        "숙소": max(2, min(days, 4)),
    }
    for category, limit in limits.items():
        candidates = [p for p in places if p.category == category and not p.is_required]
        # Keep both preference search relevance and geographic variety. Stronger
        # proximity preference adds the nearest alternatives to required locations.
        if required and request.preference.distance_preference is not None:
            nearest = sorted(
                candidates, key=lambda p: min(distance_km(p, r) for r in required)
            )
            nearby_count = round(
                limit * (100 - request.preference.distance_preference) / 100
            )
            chosen = nearest[:nearby_count]
            chosen_ids = {p.provider_place_id for p in chosen}
            chosen += [p for p in candidates if p.provider_place_id not in chosen_ids][
                : limit - len(chosen)
            ]
        else:
            chosen = candidates[:limit]
        result.extend(chosen)
    return result


def build_context(request: ItineraryRequest, places: list[Place]) -> dict:
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    return {
        "request": request.model_dump(mode="json"),
        "required_order": [
            p.provider_place_id
            for p in sorted(request.required_places, key=lambda p: p.order)
        ],
        "day_windows": day_windows(request),
        "stay_minutes": {k: policy[k] for k in ("관광", "식당", "숙소")},
        "candidates": [
            p.model_dump(exclude={"road_address", "provider", "is_required"})
            for p in places
        ],
        "travel_edges": {
            "description": "Estimated minutes. Matrix row/column order follows place_ids; use for every consecutive pair, including next-day first item.",
            "place_ids": [p.provider_place_id for p in places],
            "minutes": [
                [
                    travel_minutes(a, b, request.preference.transport_type)
                    for b in places
                ]
                for a in places
            ],
        },
    }


def schedule_selection(
    request: ItineraryRequest, selection: ModelSelection, places: list[Place]
) -> ModelItinerary:
    """Turn the model's ordered places into a feasible minute-precision schedule."""
    windows = day_windows(request)
    if [d.date for d in selection.days] != [w["date"] for w in windows]:
        raise InvalidModelOutput(
            "include ALL dates exactly once: " + ", ".join(w["date"] for w in windows)
        )
    by_id = {p.provider_place_id: p for p in places}
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    days = []
    previous = None
    for selected, window in zip(selection.days, windows):
        chosen = []
        transfers = []
        for item in selected.items:
            place = by_id.get(item.provider_place_id)
            if place is None:
                raise InvalidModelOutput("use only provided candidate IDs")
            chosen.append(place)
            transfers.append(
                travel_minutes(previous, place, request.preference.transport_type)
                if previous
                else 0
            )
            previous = place
        stays = [policy[p.category][0] for p in chosen]
        spare = window["available_minutes"] - sum(stays) - sum(transfers)
        if spare < 0:
            raise InvalidModelOutput(
                f"{selected.date}: travel + minimum visits exceed available time by {-spare} minutes; choose fewer or closer places"
            )
        # Prefer the midpoint of each pace range; shorten only within that range.
        for index, place in enumerate(chosen):
            target = sum(policy[place.category]) // 2
            extra = min(target - stays[index], spare)
            stays[index] += extra
            spare -= extra
        cursor = datetime.fromisoformat(f"{selected.date}T{window['start']}")
        items = []
        for index, (item, place) in enumerate(zip(selected.items, chosen)):
            cursor += timedelta(minutes=transfers[index])
            # Move meals toward a natural lunch/dinner window when the day has slack.
            if place.category == "식당":
                meal_hour = (
                    11 if cursor.hour < 11 else 17 if 14 <= cursor.hour < 17 else None
                )
                if meal_hour is not None:
                    meal_time = cursor.replace(hour=meal_hour, minute=0)
                    wait = int((meal_time - cursor).total_seconds() / 60)
                    if wait <= spare:
                        cursor = meal_time
                        spare -= wait
            items.append(
                {
                    "provider_place_id": item.provider_place_id,
                    "start_time": cursor.strftime("%H:%M"),
                    "stay_minutes": stays[index],
                }
            )
            cursor += timedelta(minutes=stays[index])
        days.append({"date": selected.date, "items": items})
    return ModelItinerary(title=selection.title, days=days)


def validate_itinerary(
    request: ItineraryRequest,
    generated: ModelItinerary,
    places: list[Place],
) -> list[ItineraryDay]:
    windows = day_windows(request)
    if [day.date for day in generated.days] != [w["date"] for w in windows]:
        raise InvalidModelOutput(
            "days must contain every requested date exactly once in order"
        )
    by_id = {p.provider_place_id: p for p in places}
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    seen: set[str] = set()
    first_visits: list[str] = []
    result = []
    previous_place = None
    recommended = False
    for model_day, window in zip(generated.days, windows):
        start = datetime.fromisoformat(f"{window['date']}T{window['start']}")
        end = datetime.fromisoformat(f"{window['date']}T{window['end']}")
        if not window["min_items"] <= len(model_day.items) <= window["max_items"]:
            raise InvalidModelOutput(
                f"{window['date']}: item count must be {window['min_items']}..{window['max_items']}"
            )
        previous_end = start
        categories = set()
        items = []
        for sequence, item in enumerate(model_day.items, 1):
            place = by_id.get(item.provider_place_id)
            if place is None:
                raise InvalidModelOutput("use only provided candidate IDs")
            if place.provider_place_id in seen and place.category != "숙소":
                raise InvalidModelOutput(
                    "tourist/restaurant places must not be repeated"
                )
            if place.category == "숙소" and sequence != len(model_day.items):
                raise InvalidModelOutput(
                    "accommodation must be the last item of the day"
                )
            if place.category in categories and place.category == "숙소":
                raise InvalidModelOutput("at most one accommodation per day")
            minimum, maximum = policy[place.category]
            if not minimum <= item.stay_minutes <= maximum:
                raise InvalidModelOutput(
                    f"stay_minutes for {place.category} must be {minimum}..{maximum}"
                )
            visit_start = datetime.fromisoformat(f"{window['date']}T{item.start_time}")
            visit_end = visit_start + timedelta(minutes=item.stay_minutes)
            transfer = (
                travel_minutes(previous_place, place, request.preference.transport_type)
                if previous_place
                else 0
            )
            earliest = previous_end + timedelta(minutes=transfer)
            if visit_start < earliest or visit_end > end:
                raise InvalidModelOutput(
                    f"{window['date']} item {sequence}: start at/after {earliest.strftime('%H:%M')}, "
                    f"end at/before {window['end']}; travel needs {transfer} minutes"
                )
            if place.provider_place_id not in seen:
                first_visits.append(place.provider_place_id)
            seen.add(place.provider_place_id)
            recommended |= not place.is_required
            categories.add(place.category)
            items.append(
                ItineraryItem(
                    **place.model_dump(exclude={"source_category"}),
                    sequence=sequence,
                    item_type={
                        "관광": "TOUR",
                        "식당": "RESTAURANT",
                        "숙소": "ACCOMMODATION",
                    }[place.category],
                    start_time=visit_start.strftime("%H:%M"),
                    end_time=visit_end.strftime("%H:%M"),
                    stay_minutes=item.stay_minutes,
                    travel_minutes_from_previous=transfer,
                )
            )
            previous_place, previous_end = place, visit_end
        if window["needs_tour_and_restaurant"] and not {"관광", "식당"}.issubset(
            categories
        ):
            raise InvalidModelOutput(
                f"{window['date']}: include a tourist place and restaurant"
            )
        if window["needs_accommodation"] and "숙소" not in categories:
            raise InvalidModelOutput(
                f"{window['date']}: include accommodation as the last item"
            )
        result.append(
            ItineraryDay(day_number=len(result) + 1, date=start.date(), items=items)
        )
    required_order = [
        p.provider_place_id
        for p in sorted(request.required_places, key=lambda p: p.order)
    ]
    missing = set(required_order) - seen
    if missing:
        raise InvalidModelOutput(
            "missing required_places: "
            + json.dumps(sorted(missing), ensure_ascii=False)
        )
    if [pid for pid in first_visits if pid in set(required_order)] != required_order:
        raise InvalidModelOutput("required_places must follow required_order")
    if not recommended:
        raise InvalidModelOutput("include at least one new Kakao recommendation")
    return result


async def generate_itinerary(
    request: ItineraryRequest,
    places_client: KakaoPlaces,
    planner: OpenAIPlanner,
) -> ItineraryResponse:
    windows = day_windows(request)
    if not any(w["max_items"] for w in windows):
        raise GenerationFailed("여행 시간 안에 장소를 방문할 여유가 없습니다.")
    if len(request.required_places) > sum(w["max_items"] for w in windows):
        raise GenerationFailed(
            "여행 기간과 속도에 비해 필수 방문 장소가 너무 많습니다."
        )
    places = select_candidates(request, await places_client.collect(request))
    categories = {p.category for p in places}
    if (
        any(w["needs_tour_and_restaurant"] for w in windows)
        and not {"관광", "식당"} <= categories
    ):
        raise GenerationFailed("관광 장소 또는 식당 후보가 부족합니다.")
    if any(w["needs_accommodation"] for w in windows) and "숙소" not in categories:
        raise GenerationFailed("숙소 후보가 부족합니다.")
    context = build_context(request, places)
    feedback = None
    for attempt in range(2):
        try:
            selection = await planner.generate(context, feedback)
            generated = schedule_selection(request, selection, places)
            days = validate_itinerary(request, generated, places)
            break
        except InvalidModelOutput as exc:
            logger.warning("Itinerary validation failed on attempt %s", attempt + 1)
            feedback = str(exc)
    else:
        raise GenerationFailed(
            "필수 장소와 시간 조건을 만족하는 일정을 생성하지 못했습니다."
        )
    return make_itinerary_response(
        request, generated, days, planner.settings.openai_model
    )


def make_itinerary_response(
    request: ItineraryRequest,
    generated: ModelItinerary,
    days: list[ItineraryDay],
    model_version: str,
) -> ItineraryResponse:
    warnings = [
        "이동시간은 좌표와 이동수단에 따른 추정치이며 실제 길찾기 결과가 아닙니다.",
        "영업시간·휴무일·입장료·예약 가능 여부는 방문 전에 확인해 주세요.",
        "숙소 체류시간은 체크인·휴식 시간입니다.",
    ]
    if (
        request.preference.budget_min is not None
        or request.preference.budget_max is not None
    ):
        warnings.append(
            "예산은 전체 인원·전체 여행의 추천 선호로 반영되며 실제 비용은 확인되지 않았습니다."
        )
    payload = request.model_dump(mode="json", exclude={"required_places"})
    payload["required_places"] = [
        {**p.model_dump(exclude={"place_name"}), "name": p.place_name}
        for p in request.required_places
    ]
    return ItineraryResponse(
        **payload,
        title=generated.title,
        days=days,
        model_version=model_version,
        warnings=warnings,
    )
