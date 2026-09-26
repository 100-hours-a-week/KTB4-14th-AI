from __future__ import annotations

from datetime import datetime, time, timedelta
import json
import logging
import math

from ai_service.errors import GenerationFailed, InvalidModelOutput
from ai_service.model import OpenAIPlanner
from ai_service.places import KakaoPlaces, distance_km, travel_minutes
from ai_service.transport import base_transport, resolve_day_transports
from ai_service.schemas import (
    ItineraryDay,
    ItineraryItem,
    ItineraryRequest,
    ItineraryResponse,
    ModelItinerary,
    ModelSelection,
    PACE_ALIASES,
    Place,
    RouteSummary,
    SelectionItem,
)


logger = logging.getLogger(__name__)
ARRIVAL_BUFFER_MINUTES = {"RELAXED": 45, "BALANCED": 30, "PACKED": 15}
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
    transports = resolve_day_transports(request)
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    windows = []
    for index in range((departure.date() - arrival.date()).days + 1):
        day = arrival.date() + timedelta(days=index)
        earliest_arrival = arrival + timedelta(minutes=ARRIVAL_BUFFER_MINUTES[PACE_ALIASES[request.preference.pace_type]])
        start = max(datetime.combine(day, time(9)), earliest_arrival if index == 0 else arrival)
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
                "transport_type": base_transport(transports[day.isoformat()]),
                "route_mode": transports[day.isoformat()],
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


def accommodation_period(cursor: datetime, end: datetime, minimum: int) -> tuple[datetime, int]:
    """Planning assumption: check in from 15:00, then rest until the day ends.

    This is not a verified property check-in time or next-day checkout time.
    """
    start = max(cursor, cursor.replace(hour=15, minute=0, second=0, microsecond=0))
    stay = int((end - start).total_seconds() / 60)
    if stay < minimum:
        raise InvalidModelOutput("leave time for accommodation from 15:00 until the daily end")
    return start, stay


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


def complete_selection(
    request: ItineraryRequest,
    selection: ModelSelection,
    places: list[Place],
    *,
    places_only: bool = False,
) -> ModelSelection:
    """Complete required categories using real candidates before time validation.

    Keep the model's title and relative visit order. Only optional places may be
    removed to make room; missing required IDs still require model correction.
    """
    windows = day_windows(request)
    if [day.date for day in selection.days] != [w["date"] for w in windows]:
        raise InvalidModelOutput("include every requested date exactly once")
    by_id = {p.provider_place_id: p for p in places}
    selected_ids = [
        item.provider_place_id for day in selection.days for item in day.items
    ]
    if any(pid not in by_id for pid in selected_ids):
        raise InvalidModelOutput("use only provided candidate IDs")
    required = {
        p.provider_place_id
        for p in places
        if p.is_required and (not places_only or p.category != "숙소")
    }
    if not required.issubset(selected_ids):
        raise InvalidModelOutput("include ALL required candidate IDs")
    used = {pid for pid in selected_ids if by_id[pid].category != "숙소"}
    seen = set()
    result = selection.model_copy(deep=True)
    previous = None
    required_day_hotel = len(windows) == 1 and any(
        p.is_required and p.category == "숙소" for p in places
    )
    for day, window in zip(result.days, windows):
        needs_hotel = window["needs_accommodation"] or required_day_hotel
        reserve = int(places_only and needs_hotel)
        minimum, maximum = (
            max(0, window["min_items"] - reserve),
            max(0, window["max_items"] - reserve),
        )
        chosen, hotels = [], []
        for item in day.items:
            place = by_id[item.provider_place_id]
            if place.category == "숙소":
                if not places_only and (needs_hotel or place.is_required):
                    hotels.append(place)
            elif place.provider_place_id not in seen:
                chosen.append(place)
                seen.add(place.provider_place_id)
        fixed_hotels = {p.provider_place_id: p for p in hotels if p.is_required}
        if len(fixed_hotels) > 1:
            raise InvalidModelOutput(
                "assign required hotels to separate eligible dates"
            )
        if hotels:
            chosen.append(
                next(iter(fixed_hotels.values())) if fixed_hotels else hotels[0]
            )
        needed = {"관광", "식당"} if window["needs_tour_and_restaurant"] else set()
        if not places_only and needs_hotel:
            needed.add("숙소")

        def add_candidate(category):
            options = [
                p
                for p in places
                if not p.is_required
                and (category is None or p.category == category)
                and (
                    p.category == "숙소" if category == "숙소" else p.category != "숙소"
                )
                and (p.category == "숙소" or p.provider_place_id not in used)
            ]
            proposals = []
            for place in options:
                last_position = len(chosen) - int(
                    bool(chosen) and chosen[-1].category == "숙소"
                )
                positions = (
                    [len(chosen)]
                    if place.category == "숙소"
                    else range(last_position + 1)
                )
                for position in positions:
                    if place.category == "식당" and (
                        (position > 0 and chosen[position - 1].category == "식당")
                        or (position < len(chosen) and chosen[position].category == "식당")
                    ):
                        continue
                    before = chosen[position - 1] if position else previous
                    after = chosen[position] if position < len(chosen) else None
                    cost = (distance_km(before, place) if before else 0) + (
                        distance_km(place, after) if after else 0
                    )
                    if before and after:
                        cost -= distance_km(before, after)
                    proposals.append((cost, len(proposals), position, place))
            if not proposals:
                raise InvalidModelOutput(
                    f"{day.date}: insufficient unused candidates for {category or 'daily visits'}"
                )
            _, _, position, place = min(proposals, key=lambda entry: entry[:2])
            chosen.insert(position, place)
            if place.category != "숙소":
                used.add(place.provider_place_id)
                seen.add(place.provider_place_id)

        for category in ("관광", "식당", "숙소"):
            if category in needed and not any(p.category == category for p in chosen):
                add_candidate(category)
        while len(chosen) > maximum:
            removable = [
                (index, place)
                for index, place in enumerate(chosen)
                if not place.is_required
                and (
                    place.category not in needed
                    or sum(p.category == place.category for p in chosen) > 1
                )
            ]
            if not removable:
                raise InvalidModelOutput(
                    f"{day.date}: required visits and categories exceed daily capacity"
                )

            # Remove an optional detour, preserving all required visits/categories.
            def detour(entry):
                index, place = entry
                before = chosen[index - 1] if index else previous
                after = chosen[index + 1] if index + 1 < len(chosen) else None
                return (distance_km(before, place) if before else 0) + (
                    distance_km(place, after) if after else 0
                )

            index, _ = max(removable, key=detour)
            chosen.pop(index)
        # A second optional meal is not a substitute for a missing attraction.
        # Required meals are never silently dropped; the planner must separate them.
        index = 1
        while index < len(chosen):
            before, current = chosen[index - 1], chosen[index]
            if before.category == current.category == "식당":
                if before.is_required and current.is_required:
                    raise InvalidModelOutput("separate required restaurants with tourist visits or different days")
                removed = chosen.pop(index if not current.is_required else index - 1)
                used.discard(removed.provider_place_id)
                seen.discard(removed.provider_place_id)
                index = max(1, index - 1)
            else:
                index += 1
        while len(chosen) < minimum:
            add_candidate(None)
        day.items = [
            SelectionItem(provider_place_id=p.provider_place_id) for p in chosen
        ]
        previous = chosen[-1] if chosen else previous
    return result


def build_context(request: ItineraryRequest, places: list[Place]) -> dict:
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    windows = day_windows(request)
    matrices = {
        mode: [[travel_minutes(a, b, mode) for b in places] for a in places]
        for mode in {w["transport_type"] for w in windows}
    }
    return {
        "request": request.model_dump(mode="json"),
        "required_order": [
            p.provider_place_id
            for p in sorted(request.required_places, key=lambda p: p.order)
        ],
        "day_windows": windows,
        "stay_minutes": {k: policy[k] for k in ("관광", "식당", "숙소")},
        "candidates": [
            p.model_dump(exclude={"road_address", "provider", "is_required"})
            for p in places
        ],
        "travel_edges": {
            "description": "Estimated minutes by date. Use the destination day's matrix, including transfers from the previous night's lodging. Row/column order follows place_ids.",
            "place_ids": [p.provider_place_id for p in places],
            "minutes_by_date": {w["date"]: matrices[w["transport_type"]] for w in windows},
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
                travel_minutes(previous, place, window["transport_type"])
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
        end = datetime.fromisoformat(f"{selected.date}T{window['end']}")
        items = []
        for index, (item, place) in enumerate(zip(selected.items, chosen)):
            cursor += timedelta(minutes=transfers[index])
            if place.category == "숙소":
                if index != len(chosen) - 1:
                    raise InvalidModelOutput("accommodation must be the last item of the day")
                cursor, stays[index] = accommodation_period(cursor, end, policy["숙소"][0])
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
    *,
    routes: dict[tuple[int, int], RouteSummary] | None = None,
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
            if place.category == "식당" and items and items[-1].item_type == "RESTAURANT":
                raise InvalidModelOutput("restaurants must not be consecutive within a day")
            minimum, maximum = policy[place.category]
            if place.category != "숙소" and not minimum <= item.stay_minutes <= maximum:
                raise InvalidModelOutput(
                    f"stay_minutes for {place.category} must be {minimum}..{maximum}"
                )
            visit_start = datetime.fromisoformat(f"{window['date']}T{item.start_time}")
            visit_end = visit_start + timedelta(minutes=item.stay_minutes)
            if place.category == "숙소":
                if visit_start.hour < 15 or visit_end != end or item.stay_minutes < minimum:
                    raise InvalidModelOutput("accommodation must cover check-in/rest through the daily end, from 15:00 or later")
            route = (
                routes.get((len(result) + 1, sequence)) if routes is not None else None
            )
            if previous_place and routes is not None and route is None:
                raise InvalidModelOutput("missing verified route")
            transfer = (
                route.duration_minutes
                if route
                else (
                    travel_minutes(
                        previous_place, place, window["transport_type"]
                    )
                    if previous_place
                    else 0
                )
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
                    **place.model_dump(exclude={"source_category", "is_required"}),
                    sequence=sequence,
                    item_type={
                        "관광": "TOUR",
                        "식당": "RESTAURANT",
                        "숙소": "ACCOMMODATION",
                    }[place.category],
                    start_time=visit_start.strftime("%H:%M"),
                    end_time=visit_end.strftime("%H:%M"),
                    route_from_previous=route,
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
            ItineraryDay(day_number=len(result) + 1, travel_date=start.date(), items=items)
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


def validate_generation_window(request: ItineraryRequest) -> list[dict]:
    windows = day_windows(request)
    if not any(w["max_items"] for w in windows):
        raise GenerationFailed("여행 시간 안에 장소를 방문할 여유가 없습니다.", reason="insufficient_trip_time")
    if len(request.required_places) > sum(w["max_items"] for w in windows):
        raise GenerationFailed(
            "여행 기간과 속도에 비해 필수 방문 장소가 너무 많습니다.", reason="too_many_required_places"
        )
    return windows


async def generate_itinerary(
    request: ItineraryRequest,
    places_client: KakaoPlaces,
    planner: OpenAIPlanner,
    router=None,
) -> ItineraryResponse:
    windows = validate_generation_window(request)
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
            selection = complete_selection(request, selection, places)
            from ai_service.routing import KakaoRoutes, schedule_with_routes

            router = router or KakaoRoutes(planner.client, planner.settings)
            result = await schedule_with_routes(
                request, selection, places, planner.settings.openai_model, router
            )
            return result.itinerary
        except InvalidModelOutput as exc:
            logger.warning(
                "Itinerary validation failed on attempt %s: %s", attempt + 1, str(exc)
            )
            feedback = str(exc)
    else:
        raise GenerationFailed(
            "필수 장소와 시간 조건을 만족하는 일정을 생성하지 못했습니다."
        )


def make_itinerary_response(
    request: ItineraryRequest,
    generated: ModelItinerary,
    days: list[ItineraryDay],
    model_version: str,
    *,
    routed: bool = False,
) -> ItineraryResponse:
    logger.debug("Itinerary generated model=%s routed=%s", model_version, routed)
    payload = request.model_dump(mode="json", exclude={"required_places"})
    payload["required_places"] = [
        p.model_dump()
        for p in request.required_places
    ]
    return ItineraryResponse(
        **payload,
        title=generated.title,
        days=days,
    )
