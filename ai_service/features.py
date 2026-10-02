from __future__ import annotations

from datetime import datetime, time, timedelta
from itertools import combinations, permutations
import json
import logging
import math

from ai_service.errors import GenerationFailed, InvalidModelOutput
from ai_service.group_policy import daily_item_reduction, movement_buffer_minutes, proximity_floor
from ai_service.places import distance_km, travel_minutes
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


# 장소 선택은 09~21시 안에서 계획해 숙소·실제 이동에 쓸 여유를 남긴다.
# 숙소와 경로를 넣은 최종 일정은 같은 날짜의 23:59까지 허용한다.
# 하루 장소 수 제한은 두 단계 모두 PLACE_DAY 기준으로 계산한다.
PLACE_DAY = (time(9), time(21))
SCHEDULE_DAY = (time(9), time(23, 59))


def day_windows(request: ItineraryRequest, *, schedule: bool = False) -> list[dict]:
    """날짜별 방문 가능 시간과 장소 수 제한을 구한다.

    schedule=True이면 시간만 넓히고 장소 수 제한은 유지한다.
    """
    windows = _day_windows(request, PLACE_DAY)
    if schedule:
        for window, wide in zip(windows, _day_windows(request, SCHEDULE_DAY)):
            window.update(start=wide["start"], end=wide["end"],
                          available_minutes=wide["available_minutes"])
    return windows


def _day_windows(request: ItineraryRequest, bounds: tuple[time, time]) -> list[dict]:
    """도착·출발 시각과 날짜별 이동수단을 반영한 하루 시간 창을 만든다."""
    day_start, day_end = bounds
    arrival, departure = request.duration.local_bounds()
    transports = resolve_day_transports(request)
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    windows = []
    for index in range((departure.date() - arrival.date()).days + 1):
        day = arrival.date() + timedelta(days=index)
        earliest_arrival = arrival + timedelta(minutes=ARRIVAL_BUFFER_MINUTES[PACE_ALIASES[request.preference.pace_type]])
        start = max(datetime.combine(day, day_start), earliest_arrival if index == 0 else arrival)
        end = min(datetime.combine(day, day_end), departure)
        if day == arrival.date() and arrival.time() >= day_end:
            end = min(datetime.combine(day, time(23, 59)), departure)
        start = min(start, end)
        # 일정은 분 단위이므로 도착 시각을 내림해 사용 불가능한 시간을 넣지 않는다.
        if start.second or start.microsecond:
            start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        end = end.replace(second=0, microsecond=0)
        start = min(start, end)
        minutes = max(0, int((end - start).total_seconds() / 60))
        # 첫날·마지막 날이 이동만 가능한 경우에도 결과의 날짜는 유지한다.
        min_stay = min(policy[category][0] for category in ("관광", "식당", "숙소"))
        maximum = min(policy["max_items"], max(0, minutes // min_stay))
        minimum = min(maximum, math.ceil(policy["min_items"] * minutes / 720))
        if maximum:
            # 필수 장소는 밀도 축소보다 우선한다. 날짜 배분은 모델이 결정한다.
            required_floor = min(maximum, len(request.required_places))
            category_floor = 2 if minutes >= 240 else 1
            maximum = max(required_floor, min(maximum, category_floor), maximum - daily_item_reduction(request.headcount))
            minimum = min(max(1 if minimum else 0, minimum - daily_item_reduction(request.headcount)), maximum)
        # 짧은 도착·출발일에는 한 곳의 최소 체류가 가능해 보여도 이전 장소에서
        # 이동할 시간이 없을 수 있다. 선택은 허용하되 방문 1곳을 강제하지 않는다.
        shortest_visit = min(policy["관광"][0], policy["식당"][0])
        if minutes <= shortest_visit + 30:
            minimum = 0
        windows.append(
            {
                "date": day.isoformat(),
                "transport_type": base_transport(transports[day.isoformat()]),
                "route_mode": transports[day.isoformat()],
                "start": start.strftime("%H:%M"),
                "end": end.strftime("%H:%M"),
                "available_minutes": minutes,
                **({"group_transfer_buffer_minutes": movement_buffer_minutes(request.headcount)}
                   if request.headcount >= 10 else {}),
                "min_items": minimum,
                "max_items": maximum,
                "needs_accommodation": day < departure.date()
                and minutes >= policy["숙소"][0],
                "needs_tour_and_restaurant": minutes >= 240,
            }
        )
    return windows


def minimum_day_minutes(
    day_places: list[Place], window: dict, policy: dict,
    arriving_from: Place | None = None, *, reserve_lodging: bool = False,
    respect_restaurant_hours: bool = False,
) -> int:
    """장소 선택과 최종 검증에 같은 최소 체류·예상 이동 시간을 적용한다."""
    group_buffer = window.get("group_transfer_buffer_minutes", 0)
    reserve = int(reserve_lodging) * (policy["숙소"][0] + 20 + group_buffer)
    start = datetime.fromisoformat(f"{window['date']}T{window['start']}")
    end = datetime.fromisoformat(f"{window['date']}T{window['end']}")
    cursor = start
    previous = arriving_from
    for place in day_places:
        if previous is not None:
            cursor += timedelta(minutes=travel_minutes(previous, place, window["transport_type"]) + group_buffer)
        stay = policy[place.category][0]
        if (respect_restaurant_hours and place.category == "식당"
                and place._restaurant_hours is not None):
            fitted = place._restaurant_hours.next_start(cursor, stay, end)
            if fitted is None:
                return window["available_minutes"] + 1
            cursor = fitted
        cursor += timedelta(minutes=stay)
        previous = place
    return int((cursor - start).total_seconds() / 60) + reserve


def ensure_one_optional_visit(
    request: ItineraryRequest, selection: ModelSelection, places: list[Place],
) -> ModelSelection:
    """짧은 일정이 전부 비었을 때 시간에 맞는 방문 한 곳을 보충한다."""
    if any(day.items for day in selection.days):
        return selection
    windows = day_windows(request)
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    required_day_hotel = len(windows) == 1 and any(
        place.is_required and place.category == "숙소" for place in places
    )
    options = []
    for index, window in enumerate(windows):
        reserve = window["needs_accommodation"] or (
            required_day_hotel and index == len(windows) - 1
        )
        if window["max_items"] <= int(reserve):
            continue
        for place in places:
            if place.category == "숙소" or place.is_required:
                continue
            needed = minimum_day_minutes([place], window, policy,
                                         reserve_lodging=reserve,
                                         respect_restaurant_hours=True)
            if needed <= window["available_minutes"]:
                options.append((needed - window["available_minutes"], index, place))
    if options:
        _, index, place = min(options, key=lambda choice: (choice[0], choice[1]))
        selection.days[index].items.append(SelectionItem(provider_place_id=place.provider_place_id))
    return selection


def can_complete_optional_day(
    chosen: list[Place], places: list[Place], window: dict, policy: dict,
    arriving_from: Place | None, used_ids: set[str], *, minimum: int,
    required_categories: set[str], reserve_lodging: bool = False,
) -> bool:
    """부족한 선택 방문을 실제 후보로 보충할 수 있을 때만 최소 개수를 강제한다.

    선택 장소의 교체를 포함해 실제 체류·이동시간 안에 들어가는 조합을 찾는다.
    탐색 한도를 넘으면 최소 조건을 유지해 무리한 완화를 피한다.
    """
    if len(chosen) >= minimum and required_categories <= {p.category for p in chosen}:
        return True
    shortage = minimum - len(chosen)
    unused = [place for place in places
              if place.category != "숙소" and not place.is_required
              and place.provider_place_id not in used_ids]
    if len(chosen) + len(unused) < minimum:
        return False
    if not required_categories <= {p.category for p in chosen + unused}:
        return False
    optional_indices = [index for index, place in enumerate(chosen)
                        if not place.is_required and place.category != "숙소"]
    maximum = window["max_items"] - int(reserve_lodging)
    explored = 0
    search_exhausted = False

    def visit(current: list[Place], remaining: list[Place], added: int) -> bool:
        nonlocal explored, search_exhausted
        explored += 1
        if explored > 5000:
            search_exhausted = True
            return False
        if len(current) >= minimum and required_categories <= {p.category for p in current}:
            return minimum_day_minutes(current, window, policy, arriving_from,
                                       reserve_lodging=reserve_lodging,
                                       respect_restaurant_hours=True) <= window["available_minutes"]
        if len(current) >= maximum:
            return False
        insert_end = len(current) - int(bool(current) and current[-1].category == "숙소")
        for candidate in remaining:
            for position in range(insert_end + 1):
                proposal = current[:position] + [candidate] + current[position:]
                if any(a.category == b.category == "식당" for a, b in zip(proposal, proposal[1:])):
                    continue
                if minimum_day_minutes(proposal, window, policy, arriving_from,
                                       reserve_lodging=reserve_lodging,
                                       respect_restaurant_hours=True) > window["available_minutes"]:
                    continue
                if visit(proposal, [p for p in remaining if p.provider_place_id != candidate.provider_place_id], added + 1):
                    return True
        return False

    for removed_count in range(min(2, len(optional_indices)) + 1 if shortage <= 2 else 1):
        for removed in combinations(optional_indices, removed_count):
            seed = [place for index, place in enumerate(chosen) if index not in removed]
            reusable_ids = {chosen[index].provider_place_id for index in removed}
            options = [place for place in places
                       if place.category != "숙소" and not place.is_required
                       and (place.provider_place_id not in used_ids
                            or place.provider_place_id in reusable_ids)]
            if visit(seed, options, 0):
                return True
    return search_exhausted


def hotel_rest_end(start: datetime, end: datetime, minimum: int) -> datetime:
    """숙소 휴식 종료를 보통 21시로 두되 늦은 체크인에는 최소 체류를 보장한다."""
    usual = start.replace(hour=PLACE_DAY[1].hour, minute=PLACE_DAY[1].minute, second=0, microsecond=0)
    return min(end, max(usual, start + timedelta(minutes=minimum)))


def accommodation_period(cursor: datetime, end: datetime, minimum: int) -> tuple[datetime, int]:
    """15시 이후 체크인과 휴식을 일정에 배치한다.

    실제 숙소의 체크인·체크아웃 정책을 확인한 값은 아니다.
    """
    start = max(cursor, cursor.replace(hour=15, minute=0, second=0, microsecond=0))
    stay = int((hotel_rest_end(start, end, minimum) - start).total_seconds() / 60)
    if stay < minimum:
        raise InvalidModelOutput("leave time for accommodation from 15:00 until the daily end")
    return start, stay


def select_candidates(request: ItineraryRequest, places: list[Place]) -> list[Place]:
    """필수 장소를 보존하면서 카테고리별 후보 수를 제한한다."""
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
        # 필수 장소와 가까운 후보를 반영하되 검색 관련성과 지역 다양성도 유지한다.
        anchors = required or (places[:1] if request.headcount >= 10 else [])
        if anchors and (request.preference.distance_preference is not None or request.headcount >= 5):
            nearest = sorted(
                candidates, key=lambda p: min(distance_km(p, r) for r in anchors)
            )
            nearby_count = round(
                limit * max(
                    (100 - request.preference.distance_preference) / 100
                    if request.preference.distance_preference is not None else 0,
                    proximity_floor(request.headcount),
                )
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


def optimize_group_order(
    request: ItineraryRequest, selection: ModelSelection, places: list[Place]
) -> ModelSelection:
    """선택된 장소만 재정렬해 단체 여행의 불필요한 왕복을 줄인다."""
    if request.headcount < 5:
        return selection
    by_id = {place.provider_place_id: place for place in places}
    required_order = {place.provider_place_id: place.order for place in request.required_places}
    result = selection.model_copy(deep=True)
    previous = None
    for day in result.days:
        original = [by_id[item.provider_place_id] for item in day.items]
        if len(original) < 3:
            previous = original[-1] if original else previous
            continue
        distances = {}

        def km(a, b):
            key = (a.provider_place_id, b.provider_place_id)
            if key not in distances:
                distances[key] = distance_km(a, b)
            return distances[key]

        def valid(path):
            orders = [required_order[p.provider_place_id] for p in path if p.provider_place_id in required_order]
            return (
                orders == sorted(orders)
                and all(p.category != "숙소" or i == len(path) - 1 for i, p in enumerate(path))
                and all(a.category != b.category or a.category != "식당" for a, b in zip(path, path[1:]))
            )

        def score(path):
            points = ([previous] if previous else []) + list(path)
            legs = [km(a, b) for a, b in zip(points, points[1:])]
            # 가까운 곳으로 돌아오는 경로는 같은 지역을 반복 방문하는 것으로 본다.
            backtrack = sum(
                min(km(a, b), km(b, c))
                for a, b, c in zip(points, points[1:], points[2:])
                if km(a, c) < min(km(a, b), km(b, c)) * 0.5
            )
            return sum(legs) + backtrack * (0.25 if request.headcount < 10 else 0.5)

        if not valid(original):
            previous = original[-1]
            continue
        best, best_score = original, score(original)
        # 하루 최대 8곳이라 전체 순열을 살펴도 범위가 작다.
        for path in permutations(original):
            if not valid(path):
                continue
            candidate_score = score(path)
            if candidate_score < best_score:
                best, best_score = path, candidate_score
        current_score = score(original)
        threshold = 0.85 if request.headcount < 10 else 0.95 if request.headcount < 20 else 1.0
        if best_score < current_score * threshold:
            day.items = [SelectionItem(provider_place_id=p.provider_place_id) for p in best]
        previous = by_id[day.items[-1].provider_place_id]
    return result


def complete_selection(
    request: ItineraryRequest,
    selection: ModelSelection,
    places: list[Place],
    *,
    places_only: bool = False,
) -> ModelSelection:
    """실제 후보로 빠진 카테고리를 채우고 장소 수를 조정한다.

    모델이 정한 방문 순서와 필수 장소는 유지한다.
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
                if places_only and category != "숙소":
                    return False
                raise InvalidModelOutput(
                    f"{day.date}: insufficient unused candidates for {category or 'daily visits'}"
                )
            _, _, position, place = min(proposals, key=lambda entry: entry[:2])
            chosen.insert(position, place)
            if place.category != "숙소":
                used.add(place.provider_place_id)
                seen.add(place.provider_place_id)
            return True

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

            # 필수 방문과 카테고리를 지키며 불필요한 우회 장소를 제거한다.
            def detour(entry):
                index, place = entry
                before = chosen[index - 1] if index else previous
                after = chosen[index + 1] if index + 1 < len(chosen) else None
                return (distance_km(before, place) if before else 0) + (
                    distance_km(place, after) if after else 0
                )

            index, _ = max(removable, key=detour)
            chosen.pop(index)
        # 선택 식당을 늘려 관광 장소 부족을 메우지 않으며 필수 식당은 제거하지 않는다.
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
            if not add_candidate(None):
                break
        day.items = [
            SelectionItem(provider_place_id=p.provider_place_id) for p in chosen
        ]
        previous = chosen[-1] if chosen else previous
    return ensure_one_optional_visit(request, result, places) if places_only else result


def build_context(request: ItineraryRequest, places: list[Place]) -> dict:
    """모델에 전달할 날짜별 시간 창·후보 장소·이동시간 행렬을 만든다."""
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    windows = day_windows(request)
    matrices = {
        mode: [[travel_minutes(a, b, mode) + (movement_buffer_minutes(request.headcount) if a.provider_place_id != b.provider_place_id else 0)
                for b in places] for a in places]
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
            "description": (
                "Estimated planning minutes including group movement slack, not provider route duration. Use the destination day's matrix, including transfers from the previous night's lodging. Row/column order follows place_ids."
                if request.headcount >= 10 else
                "Estimated minutes by date. Use the destination day's matrix, including transfers from the previous night's lodging. Row/column order follows place_ids."
            ),
            "place_ids": [p.provider_place_id for p in places],
            "minutes_by_date": {w["date"]: matrices[w["transport_type"]] for w in windows},
        },
    }


def schedule_selection(
    request: ItineraryRequest, selection: ModelSelection, places: list[Place]
) -> ModelItinerary:
    """모델이 정한 장소 순서를 분 단위의 실행 가능한 일정으로 바꾼다."""
    from ai_service.restaurant_hours import fit_restaurant
    windows = day_windows(request, schedule=True)
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
                + window.get("group_transfer_buffer_minutes", 0)
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
        # 체류시간은 속도별 범위의 중간값을 우선하고 여유가 없으면 최소값을 쓴다.
        for index, place in enumerate(chosen):
            target = sum(policy[place.category]) // 2
            extra = min(target - stays[index], spare)
            stays[index] += extra
            spare -= extra
        cursor = datetime.fromisoformat(f"{selected.date}T{window['start']}")
        end = datetime.fromisoformat(f"{selected.date}T{window['end']}")
        items = []
        waiting_restaurant_id = None
        for index, (item, place) in enumerate(zip(selected.items, chosen)):
            cursor += timedelta(minutes=transfers[index])
            if place.category == "숙소":
                if index != len(chosen) - 1:
                    raise InvalidModelOutput("accommodation must be the last item of the day")
                cursor, stays[index] = accommodation_period(cursor, end, policy["숙소"][0])
            # 남는 시간이 있으면 식사를 점심·저녁 시간대로 늦춘다.
            if place.category == "식당":
                meal_hour = (
                    11 if cursor.hour < 11 else 17 if 14 <= cursor.hour < 17 else None
                )
                if meal_hour is not None:
                    meal_time = cursor.replace(hour=meal_hour, minute=0)
                    wait = int((meal_time - cursor).total_seconds() / 60)
                    if wait <= spare and (
                        place._restaurant_hours is None or
                        place._restaurant_hours.next_start(meal_time, stays[index], end) == meal_time
                    ):
                        cursor = meal_time
                        spare -= wait
                try:
                    fitted = fit_restaurant(place, cursor, stays[index], end)
                except ValueError as exc:
                    raise InvalidModelOutput(str(exc)) from exc
                wait = int((fitted - cursor).total_seconds() / 60)
                cursor, spare = fitted, max(0, spare - wait)
                if wait:
                    waiting_restaurant_id = place.provider_place_id
            if cursor + timedelta(minutes=stays[index]) > end:
                raise InvalidModelOutput(
                    f"restaurant_closed:{waiting_restaurant_id}" if waiting_restaurant_id
                    else f"{selected.date}: visits exceed available time"
                )
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
    """일정의 날짜·필수 장소·방문 순서·체류시간·경로를 검증한다."""
    from ai_service.restaurant_hours import validate_restaurant_visit
    windows = day_windows(request, schedule=True)
    planning_windows = day_windows(request)
    if [day.date for day in generated.days] != [w["date"] for w in windows]:
        raise InvalidModelOutput(
            "days must contain every requested date exactly once in order"
        )
    by_id = {p.provider_place_id: p for p in places}
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    selected_ids = {
        item.provider_place_id for day in generated.days for item in day.items
    }
    seen: set[str] = set()
    first_visits: list[str] = []
    result = []
    previous_place = None
    recommended = False
    for model_day, window, planning_window in zip(generated.days, windows, planning_windows):
        start = datetime.fromisoformat(f"{window['date']}T{window['start']}")
        end = datetime.fromisoformat(f"{window['date']}T{window['end']}")
        day_places = [by_id.get(item.provider_place_id) for item in model_day.items]
        if any(place is None for place in day_places):
            raise InvalidModelOutput("use only provided candidate IDs")
        required_categories = {"관광", "식당"} if window["needs_tour_and_restaurant"] else set()
        has_shortfall = (len(model_day.items) < window["min_items"] or
                         not required_categories <= {place.category for place in day_places})
        can_complete = not has_shortfall or can_complete_optional_day(
            day_places, places, planning_window, policy, previous_place, selected_ids,
            minimum=window["min_items"], required_categories=required_categories,
        )
        if (len(model_day.items) > window["max_items"] or
                len(model_day.items) < window["min_items"] and can_complete):
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
            if place.category == "식당" and not validate_restaurant_visit(place, visit_start, visit_end):
                raise InvalidModelOutput(f"restaurant_closed:{place.provider_place_id}")
            if place.category == "숙소":
                if (visit_start.hour < 15 or visit_end != hotel_rest_end(visit_start, end, minimum)
                        or item.stay_minutes < minimum):
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
            earliest = previous_end + timedelta(
                minutes=transfer + (window.get("group_transfer_buffer_minutes", 0) if previous_place else 0)
            )
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
        if window["needs_tour_and_restaurant"] and not {"관광", "식당"}.issubset(categories) and can_complete:
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
    """외부 API 호출 전 여행 기간과 필수 장소 수의 기본 가능성을 확인한다."""
    windows = day_windows(request)
    if not any(w["max_items"] for w in windows):
        raise GenerationFailed("여행 시간 안에 장소를 방문할 여유가 없습니다.", reason="insufficient_trip_time")
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    shortest_visit = min(policy["관광"][0], policy["식당"][0])
    required_day_hotel = len(windows) == 1 and any(
        place.category == "숙소" for place in request.required_places
    )
    if not any(
        w["max_items"] > int(w["needs_accommodation"] or required_day_hotel)
        and w["available_minutes"] >= shortest_visit
        for w in windows
    ):
        raise GenerationFailed("여행 시간 안에 장소를 방문할 여유가 없습니다.", reason="insufficient_trip_time")
    if len(request.required_places) > sum(w["max_items"] for w in windows):
        raise GenerationFailed(
            "여행 기간과 속도에 비해 필수 방문 장소가 너무 많습니다.", reason="too_many_required_places"
        )
    return windows


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
