"""일정 생성의 네 단계를 조정한다.

여행 시간 사전 검증 후 장소 선택 → 숙소 선택 → 실제 경로 반영 → 음악 추천 순서로 진행한다.
장소 선택은 12시간 창에서 검증하고 한 번 수정할 수 있다. 숙소와 경로는 더 넓은
일정 창에서 전체 여행 가능 여부를 다시 검사한다. 음악 추천 실패에는 대체곡을 쓴다.
실패 원인은 pipeline_failed 로그의 request_id, reason, detail로 추적한다.

처음 읽을 때는 맨 아래 generate_plan()에서 전체 호출 순서를 먼저 확인하면 된다.
그 위의 함수들은 각 단계에서 사용하는 검증·보정·대체 탐색 도구다.

이 파일에서 자주 쓰는 값:
- request/body: 사용자가 보낸 여행 기간, 지역, 속도, 필수 장소 등의 요청.
- places/pool: 검색으로 확보한 실제 장소 정보. pool은 장소 ID로 찾는 사전이다.
- selection: 날짜별 방문할 장소 ID와 순서. 아직 확정된 시간표는 아니다.
- window: 하루에 쓸 수 있는 시작·종료 시각, 분 단위 시간, 장소 수 제한.
- required: 사용자가 지정한 필수 방문. optional은 조정 가능한 추천 방문이다.

장소·숙소 단계에서는 예상 이동시간을 사용하고, 경로 단계에서 조회한 이동시간으로
다시 검증한다. fallback은 실패 시 시도하는 대체 처리이며, 통과를 보장하지 않는다.
"""
from __future__ import annotations

from contextlib import aclosing
from itertools import combinations, combinations_with_replacement, islice, permutations
import time

from ai_service.diagnostics import failure, record
from ai_service.errors import ApiError, GenerationFailed, InvalidModelOutput
from ai_service.features import (
    PACE_POLICIES,
    build_context,
    can_complete_optional_day,
    complete_selection,
    day_windows,
    ensure_one_optional_visit,
    minimum_day_minutes,
    validate_generation_window,
    schedule_selection,
    select_candidates,
    validate_itinerary,
)
from ai_service.model import OpenAIPlanner
from ai_service.music import fallback_music
from ai_service.places import KakaoPlaces, distance_km
from ai_service.routing import KakaoRoutes, schedule_with_routes
from ai_service.schemas import (
    Coordinate,
    GenerationResult,
    ItineraryRequest,
    ModelSelection,
    PACE_ALIASES,
    Place,
    RoutesResult,
    SelectionItem,
)


def lodging_days(request: ItineraryRequest, places: list[Place]) -> set[int]:
    """숙박이 필요한 날짜와 당일 방문용 필수 숙소 날짜를 구한다."""
    windows = day_windows(request)
    # 날짜 자체가 아니라 selection.days의 인덱스를 반환한다. 0은 첫날, 1은 둘째 날이다.
    # 실제 숙박 필요 여부는 도착·출발 시각 등을 반영하는 day_windows가 계산한다.
    days = {i for i, w in enumerate(windows) if w["needs_accommodation"]}
    # 1일 여행에서도 필수 숙소는 당일 방문 장소일 수 있다.
    if not days and any(p.is_required and p.category == "숙소" for p in places):
        days.add(len(windows) - 1)
    return days


def required_hotels_by_day(
    request: ItineraryRequest,
    selection: ModelSelection,
    places: list[Place],
) -> dict[int, Place]:
    """필수 숙소의 방문 순서를 지키며 각 숙소를 배치할 날짜를 결정한다."""
    # by_id는 ID → 실제 장소, order는 ID → 사용자가 지정한 필수 방문 순서다.
    by_id = {p.provider_place_id: p for p in places}
    order = {p.provider_place_id: p.order for p in request.required_places}
    # 관광지·식당이 어느 날짜에 배치되었는지를 기준으로 숙소가 들어갈 틈을 찾는다.
    visits = {
        item.provider_place_id: index
        for index, day in enumerate(selection.days)
        for item in day.items
    }
    result = {}
    for place in sorted(
        (p for p in places if p.is_required and p.category == "숙소"),
        key=lambda p: order[p.provider_place_id],
    ):
        # 이 숙소보다 먼저/나중에 방문해야 할 필수 장소들의 날짜를 각각 모은다.
        before = [
            day
            for pid, day in visits.items()
            if pid in order and order[pid] < order[place.provider_place_id]
        ]
        after = [
            day
            for pid, day in visits.items()
            if pid in order and order[pid] > order[place.provider_place_id]
        ]
        eligible = [
            day
            for day in sorted(lodging_days(request, places))
            if day not in result
            and day >= max(before, default=-1)
            and day < min(after, default=len(selection.days))
        ]
        # 숙소는 하루의 마지막 방문이므로 앞선 필수 장소와 같은 날은 가능하지만,
        # 뒤에 방문해야 할 필수 장소와 같은 날에 놓으면 순서가 뒤집힌다.
        # day not in result 조건은 필수 숙소 두 개가 같은 날에 배치되는 것을 막는다.
        if not eligible:
            raise InvalidModelOutput(
                "move required tourist/restaurant visits to dates that leave room for required hotels in required order"
            )
        # 조건을 만족하는 날짜 중 가장 이른 날짜를 사용한다.
        result[eligible[0]] = by_id[place.provider_place_id]
    return result


def places_day_minutes(
    day_places: list[Place], window: dict, reserve: int, policy: dict,
    arriving_from: Place | None = None,
) -> int:
    """장소 체류·예상 이동·숙소 여유를 합쳐 하루 최소 소요 시간을 구한다.

    다음 날 첫 이동은 전날 마지막 장소를 숙소 위치의 근사값으로 사용한다.
    자동 축소와 검증 단계에서 같은 계산을 사용한다.
    """
    # reserve는 숙소 몫을 비워 둘지 나타내는 0/1 값이다. 시간(분) 자체가 아니다.
    # 실제 체류·이동·숙소 여유 계산은 features의 공통 함수에 맡겨 기준을 맞춘다.
    return minimum_day_minutes(
        day_places, window, policy, arriving_from, reserve_lodging=bool(reserve)
    )


def trim_places_to_time(
    request: ItineraryRequest, selection: ModelSelection, places: list[Place]
) -> ModelSelection:
    """하루 시간이 부족하면 가장 많은 시간을 절약하는 선택 장소부터 줄인다.

    필수 장소와 식당 연속 방문을 유발하는 삭제는 제외한다.
    카테고리·최소 개수 충족 여부는 뒤의 검증 단계에서 다시 판단한다.
    그래도 시간이 부족하면 검증 단계에서 필요한 시간을 보고한다.
    """
    windows = day_windows(request)
    if [d.date for d in selection.days] != [w["date"] for w in windows]:
        return selection  # 날짜 불일치는 검증 단계에서 모델에 설명한다.
    by_id = {p.provider_place_id: p for p in places}
    lodging = lodging_days(request, places)
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    # 원본 모델 응답을 건드리지 않도록 날짜와 items까지 깊게 복사한다.
    result = selection.model_copy(deep=True)
    arriving_from = None
    for index, (day, window) in enumerate(zip(result.days, windows)):
        if any(item.provider_place_id not in by_id for item in day.items):
            # 알 수 없는 ID는 임의로 삭제하지 않고 이후 검증에서 오류로 처리한다.
            continue
        reserve = int(index in lodging)
        chosen = [by_id[item.provider_place_id] for item in day.items]
        def minutes(day_places, start=arriving_from):
            return places_day_minutes(day_places, window, reserve, policy, start)

        before = minutes(chosen)
        dropped = []
        while minutes(chosen) > window["available_minutes"]:
            # 각 선택 장소를 하나씩 뺐을 때의 총 소요 시간을 비교한다.
            # 체류시간뿐 아니라 방문 순서 변경에 따른 이동시간 차이도 반영된다.
            candidates = []
            for i, place in enumerate(chosen):
                if place.is_required:
                    continue
                rest = chosen[:i] + chosen[i + 1 :]
                if any(a.category == b.category == "식당" for a, b in zip(rest, rest[1:])):
                    continue
                candidates.append((minutes(rest), i))
            if not candidates:
                # 더 줄이려면 필수 장소 등을 훼손해야 하므로 여기서 멈춘다.
                break
            _, i = min(candidates)
            dropped.append(chosen.pop(i).provider_place_id)
        if dropped:
            record(
                "place_selection_trimmed", date=day.date,
                available_minutes=window["available_minutes"],
                needed_minutes_before=before,
                needed_minutes_after=minutes(chosen),
                dropped_place_ids=dropped, remaining=len(chosen),
            )
            day.items = [SelectionItem(provider_place_id=p.provider_place_id) for p in chosen]
        arriving_from = chosen[-1] if chosen else arriving_from
    # 축소 후 관광·식당 방문이 하나도 없다면 가능한 선택 방문 한 곳을 보완한다.
    return ensure_one_optional_visit(request, result, places)


def places_budget_report(
    request: ItineraryRequest, selection: ModelSelection | None, places: list[Place]
) -> list[dict]:
    """날짜별 가용 시간과 필요 시간을 로그용으로 요약한다."""
    # 모델이 응답 구조부터 잘못 반환하면 selection이 None일 수도 있다.
    # 이 경우에도 날짜별 제한은 남겨 실패 원인을 살펴볼 수 있게 한다.
    windows = day_windows(request)
    by_id = {p.provider_place_id: p for p in places}
    lodging = lodging_days(request, places)
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    days = selection.days if selection is not None else []
    report = []
    arriving_from = None
    for index, window in enumerate(windows):
        entry = {
            "date": window["date"], "window": f"{window['start']}-{window['end']}",
            "transport": window["transport_type"],
            "available_minutes": window["available_minutes"],
            "lodging": index in lodging,
            "items_range": [window["min_items"], window["max_items"]],
        }
        if index < len(days):
            chosen = [by_id[i.provider_place_id] for i in days[index].items if i.provider_place_id in by_id]
            entry["items"] = len(days[index].items)
            entry["categories"] = [p.category for p in chosen]
            entry["needed_minutes"] = places_day_minutes(
                chosen, window, int(index in lodging), policy, arriving_from
            )
            arriving_from = chosen[-1] if chosen else arriving_from
        report.append(entry)
    return report


def validate_places_selection(
    request: ItineraryRequest, selection: ModelSelection, places: list[Place]
) -> None:
    """숙소 검색 전 장소 선택 결과의 필수 조건을 검증한다.

    실패 이유는 모델의 수정 요청에 다시 사용된다.
    """
    windows = day_windows(request)
    # 누락·중복·순서 변경을 한 번에 확인한다. 이동만 하는 날도 날짜는 필요하다.
    if [d.date for d in selection.days] != [w["date"] for w in windows]:
        raise InvalidModelOutput("include every requested date exactly once in order")
    by_id = {p.provider_place_id: p for p in places}
    lodging = lodging_days(request, places)
    # seen은 여행 전체의 방문 순서를 보존한다. selected_ids는 보완 가능성 검사에서
    # 다른 날짜에 이미 사용한 장소를 다시 후보로 넣지 않기 위해 사용한다.
    seen = []
    selected_ids = {
        item.provider_place_id for selected_day in selection.days for item in selected_day.items
    }
    arriving_from = None
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    for index, (day, window) in enumerate(zip(selection.days, windows)):
        reserve = int(index in lodging)
        # 하루 장소 수에는 숙소도 포함되므로 이 단계의 관광·식당 몫에서는 한 곳을 뺀다.
        minimum, maximum = (
            max(0, window["min_items"] - reserve),
            max(0, window["max_items"] - reserve),
        )
        categories = set()
        day_places = []
        for item in day.items:
            place = by_id.get(item.provider_place_id)
            # 모델이 만든 가짜 ID나 다음 단계에서 선택할 숙소는 여기서 허용하지 않는다.
            if place is None or place.category == "숙소":
                raise InvalidModelOutput(
                    "select only tourist/restaurant candidate IDs in this stage"
                )
            if place.provider_place_id in seen:
                raise InvalidModelOutput("tourist/restaurant places must not repeat")
            seen.append(place.provider_place_id)
            categories.add(place.category)
            day_places.append(place)
        needed_minutes = places_day_minutes(day_places, window, reserve, policy, arriving_from)
        if len(day.items) > maximum:
            raise InvalidModelOutput(
                f"{day.date}: select {minimum}..{maximum} tourist/restaurant places, leaving room for lodging"
            )
        if needed_minutes > window["available_minutes"]:
            raise InvalidModelOutput(
                f"{day.date}: choose fewer/closer places to leave time for accommodation and transfers "
                f"(needs {needed_minutes} min, window has {window['available_minutes']} min)"
            )
        required_categories = {"관광", "식당"} if window["needs_tour_and_restaurant"] else set()
        # 시간이 짧거나 후보가 부족해 채울 수 없는 최소 개수는 무조건 강제하지 않는다.
        # 보완 가능한데도 빠뜨린 경우에만 모델에게 다시 선택하라고 요청한다.
        if (len(day.items) < minimum or not required_categories <= categories) and can_complete_optional_day(
            day_places, places, window, policy, arriving_from, selected_ids,
            minimum=minimum, required_categories=required_categories,
            reserve_lodging=bool(reserve),
        ):
            if len(day.items) < minimum:
                raise InvalidModelOutput(
                    f"{day.date}: select {minimum}..{maximum} tourist/restaurant places, leaving room for lodging"
                )
            raise InvalidModelOutput(f"{day.date}: include a tourist place and a restaurant")
        arriving_from = day_places[-1] if day_places else arriving_from
    if not seen:
        raise InvalidModelOutput("include at least one tourist or restaurant candidate")
    required = [
        p.provider_place_id
        for p in sorted(request.required_places, key=lambda p: p.order)
        if by_id[p.provider_place_id].category != "숙소"
    ]
    # 전체 방문에서 필수 장소만 추려 비교하면 필수 장소의 누락과 순서 위반을 잡는다.
    if [pid for pid in seen if pid in required] != required:
        raise InvalidModelOutput(
            "include ALL required tourist/restaurant places in required_order"
        )
    # 관광·식당만으로는 유효해도 필수 숙소를 넣을 날짜가 없을 수 있어 미리 확인한다.
    required_hotels_by_day(request, selection, places)


def fallback_short_single_day(
    request: ItineraryRequest, places: list[Place],
) -> ModelSelection | None:
    """두 번의 모델 선택이 실패한 짧은 당일 여행의 유효한 후보를 찾는다."""
    windows = day_windows(request)
    # 단순한 당일 여행만 직접 탐색한다. 필수 장소가 있는 요청은 이 대체 처리에서 제외한다.
    if (len(windows) != 1 or windows[0]["min_items"] > 4
            or request.required_places):
        return None
    window = windows[0]
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    options = [place for place in places if place.category in {"관광", "식당"}]
    # 적은 장소 수부터 최대 네 곳까지 시도한다. permutations는 같은 장소 조합도
    # 방문 순서가 다르면 별도로 살펴본다. 이동시간이 순서에 따라 달라지기 때문이다.
    for count in range(1, min(4, window["max_items"]) + 1):
        for chosen in permutations(options, count):
            if (any(a.category == b.category == "식당" for a, b in zip(chosen, chosen[1:]))
                    or minimum_day_minutes(list(chosen), window, policy) > window["available_minutes"]):
                continue
            selection = ModelSelection(
                title=f"{request.region.full_name} 여행",
                days=[{"date": window["date"], "items": [
                    SelectionItem(provider_place_id=place.provider_place_id)
                    for place in chosen
                ]}],
            )
            try:
                # 장소 목록 검증에 더해 예상 이동시간으로 시간표까지 만들어 검증한다.
                # 실제 외부 경로 조회는 이후 connect_routes 단계에서 수행한다.
                validate_places_selection(request, selection, places)
                validate_itinerary(request, schedule_selection(request, selection, places), places)
            except InvalidModelOutput:
                continue
            return selection
    return None


def fallback_multi_day(
    request: ItineraryRequest, places: list[Place], selection: ModelSelection | None,
) -> ModelSelection | None:
    """모델이 여러 날짜의 장소를 과밀하게 배분하면 선택 방문을 다시 나눈다."""
    windows = day_windows(request)
    if len(windows) < 2:
        return None
    by_id = {place.provider_place_id: place for place in places}
    dates = [window["date"] for window in windows]
    assigned = {day.date: [item.provider_place_id for item in day.items]
                for day in selection.days} if selection else {}
    required_ids = [place.provider_place_id for place in sorted(
        request.required_places, key=lambda place: place.order
    ) if place.category != "숙소"]
    if any(pid not in by_id for pid in required_ids):
        return None
    # 원래 모델이 필수 장소를 배치한 날짜를 기억해 가능한 한 원안에 가깝게 고친다.
    preferred_days = [next((index for index, date in enumerate(dates)
                            if pid in assigned.get(date, [])), None)
                      for pid in required_ids]
    # 예: 필수 장소 세 곳의 배분 (0, 0, 1)은 첫날 두 곳, 둘째 날 한 곳을 뜻한다.
    # 중복을 허용하는 오름차순 조합이므로 날짜가 뒤로 갔다가 앞으로 돌아오지 않는다.
    # 경우의 수가 커지지 않도록 먼저 4,096개까지만 만들고 가까운 128개만 시험한다.
    assignments = list(islice(
        combinations_with_replacement(range(len(dates)), len(required_ids)), 4096
    ))
    if all(day is not None for day in preferred_days) and preferred_days == sorted(preferred_days):
        preferred = tuple(preferred_days)
        if preferred not in assignments:
            assignments.append(preferred)
    assignments = sorted(assignments, key=lambda days: sum(
        abs(day - preferred) for day, preferred in zip(days, preferred_days)
        if preferred is not None
    ))[:128]
    lodging = lodging_days(request, places)
    # 보완 함수가 살펴보는 후보 순서에 따라 결과가 달라질 수 있다.
    # 원래 순서, 역순, 특정 장소와 가까운 순서 등 최대 열 가지를 시험한다.
    candidate_orders = [places, list(reversed(places))]
    for anchor in (place for place in places if place.category != "숙소"):
        if len(candidate_orders) >= 10:
            break
        candidate_orders.append(sorted(places, key=lambda place: distance_km(place, anchor)))
    for days in assignments:
        # 필수 장소만으로 하루 한도를 넘기는 배분은 시간표를 만들기 전에 건너뛴다.
        if any(sum(assigned_day == index for assigned_day in days) >
               window["max_items"] - int(index in lodging)
               for index, window in enumerate(windows)):
            continue
        # 먼저 필수 장소만 있는 뼈대를 만들고, 선택 방문은 공통 보완 함수로 채운다.
        skeleton = ModelSelection(
            title=selection.title if selection else f"{request.region.full_name} 여행",
            days=[{"date": date, "items": [
                SelectionItem(provider_place_id=pid)
                for pid, assigned_day in zip(required_ids, days)
                if assigned_day == index
            ]} for index, date in enumerate(dates)],
        )
        for candidates in candidate_orders:
            try:
                completed = complete_selection(request, skeleton, candidates, places_only=True)
                completed = trim_places_to_time(request, completed, candidates)
                validate_places_selection(request, completed, candidates)
                # 영업시간 때문에 첫 후보 배치가 실패해도 다음 후보 순서를 시도한다.
                completed = replace_closed_restaurant(request, completed, places)
                schedule_selection(request, completed, places)
            except InvalidModelOutput:
                continue
            return completed
    if len(windows) == 2 and not required_ids:
        # 일반적인 재배분이 실패하면 작은 1박 여행에 한해 더 세밀한 탐색을 시도한다.
        return fallback_two_day_search(request, places, selection)
    return None


def fallback_two_day_search(
    request: ItineraryRequest, places: list[Place], selection: ModelSelection | None,
) -> ModelSelection | None:
    """적은 후보의 1박 일정에서 빈 날짜까지 포함해 방문 배분을 탐색한다."""
    visits = [place for place in places if place.category != "숙소"]
    # 방문 순열은 후보 수에 따라 급증하므로 후보 여덟 곳 이하에만 적용한다.
    if len(visits) > 8:
        return None
    windows = day_windows(request)
    lodging = lodging_days(request, places)
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    explored = 0
    # 하루 0~3곳을 시험한다. 도착이 늦거나 출발이 이르면 하루가 비어도 검증에 맡긴다.
    for first_count in range(min(3, windows[0]["max_items"] - int(0 in lodging), len(visits)) + 1):
        for first in permutations(visits, first_count):
            if places_day_minutes(list(first), windows[0], int(0 in lodging), policy) > windows[0]["available_minutes"]:
                continue
            remaining = [place for place in visits if place not in first]
            # 첫날 쓴 장소를 빼서 이틀에 같은 관광지·식당이 중복되지 않게 한다.
            for second_count in range(min(3, windows[1]["max_items"] - int(1 in lodging), len(remaining)) + 1):
                for second in permutations(remaining, second_count):
                    explored += 1
                    # 대체 탐색 때문에 요청 처리가 지나치게 길어지지 않도록 제한한다.
                    if explored > 5000:
                        return None
                    candidate = ModelSelection(
                        title=selection.title if selection else f"{request.region.full_name} 여행",
                        days=[{"date": window["date"], "items": [
                            SelectionItem(provider_place_id=place.provider_place_id)
                            for place in chosen
                        ]} for window, chosen in zip(windows, (first, second))],
                    )
                    try:
                        validate_places_selection(request, candidate, places)
                    except InvalidModelOutput:
                        continue
                    return candidate
    return None


def fallback_two_day_accommodation(
    request: ItineraryRequest, selection: ModelSelection,
    places: list[Place], hotels: list[Place],
) -> tuple[ModelSelection, list[Place]] | None:
    """소수 후보의 1박 여행에서 장소 배분과 숙소를 함께 재탐색한다."""
    windows = day_windows(request)
    # 기존 방문에서 두 곳을 빼는 정도로 숙소가 들어가지 않을 때, 1박 여행에 한해
    # 관광·식당의 날짜와 순서를 숙소 후보와 함께 다시 조합하는 마지막 대안이다.
    if len(windows) != 2 or not windows[0]["needs_accommodation"]:
        return None
    visits = [place for place in places if place.category != "숙소"]
    if len(visits) > 8:
        # 탐색할 후보를 여덟 곳으로 줄이되 필수 장소는 우선 보존한다.
        # 선택 장소는 숙소와 가까운 곳을 우선하고 관광·식당 두 분류를 확보한다.
        required = [place for place in visits if place.is_required]
        if len(required) > 8 or not hotels:
            return None
        nearby = sorted(
            (place for place in visits if not place.is_required),
            key=lambda place: min(distance_km(place, hotel) for hotel in hotels),
        )
        visits = required.copy()
        for category in ("관광", "식당"):
            if not any(place.category == category for place in visits):
                nearest = next((place for place in nearby if place.category == category), None)
                if nearest is not None and len(visits) < 8:
                    visits.append(nearest)
        visits.extend(place for place in nearby if place not in visits)  # 후보 우선순위 유지
        visits = visits[:8]
    explored = 0
    for hotel in hotels:
        # 숙소를 하나 고정한 뒤 이틀의 방문 순열을 만든다. 각 날은 최대 세 곳이다.
        pool = {place.provider_place_id: place for place in [*places, hotel]}
        for first_count in range(min(3, len(visits)) + 1):
            for first in permutations(visits, first_count):
                remaining = [place for place in visits if place not in first]
                for second_count in range(min(3, len(remaining)) + 1):
                    for second in permutations(remaining, second_count):
                        if not first and not second:
                            continue
                        explored += 1
                        # 한도는 숙소마다 초기화하지 않고 모든 후보의 누적 탐색에 적용한다.
                        if explored > 5000:
                            return None
                        proposal = ModelSelection(
                            title=selection.title,
                            days=[
                                {"date": windows[0]["date"], "items": [
                                    SelectionItem(provider_place_id=place.provider_place_id)
                                    for place in first
                                ] + [SelectionItem(provider_place_id=hotel.provider_place_id)]},
                                {"date": windows[1]["date"], "items": [
                                    SelectionItem(provider_place_id=place.provider_place_id)
                                    for place in second
                                ]},
                            ],
                        )
                        try:
                            # 첫날 마지막에 숙소를 붙인 전체 시간표로 검증한다.
                            # 필수 장소 누락·순서 위반 등도 이 검증을 통과해야 반환된다.
                            generated = schedule_selection(request, proposal, list(pool.values()))
                            validate_itinerary(request, generated, list(pool.values()))
                        except InvalidModelOutput:
                            continue
                        return proposal, list(pool.values())
    return None


def replace_closed_restaurant(
    request: ItineraryRequest, selection: ModelSelection, places: list[Place],
) -> ModelSelection:
    """Try unused, verified restaurants near a closed optional selection."""
    if not any(place._restaurant_hours is not None for place in places):
        return selection
    by_id = {place.provider_place_id: place for place in places}
    result = selection.model_copy(deep=True)
    for _ in range(sum(place.category == "식당" for place in places) + 1):
        try:
            schedule_selection(request, result, places)
            return result
        except InvalidModelOutput as exc:
            if not str(exc).startswith("restaurant_closed:"):
                raise
            closed_id = str(exc).split(":", 1)[1]
            closed = by_id.get(closed_id)
            if closed is None:
                raise
            # 후보 자체가 영업 중인 시간대가 있으면 같은 날 방문 순서를 먼저 바꾼다.
            # 필수 식당도 이 방법으로 유지할 수 있으며 기존 필수 장소 순서는 검증한다.
            rescheduled = False
            for day_index, day in enumerate(result.days):
                old_position = next((index for index, item in enumerate(day.items)
                                     if item.provider_place_id == closed_id), None)
                if old_position is None:
                    continue
                for new_position in range(len(day.items)):
                    if new_position == old_position:
                        continue
                    proposal = result.model_copy(deep=True)
                    moved = proposal.days[day_index].items.pop(old_position)
                    proposal.days[day_index].items.insert(new_position, moved)
                    try:
                        validate_places_selection(request, proposal, places)
                        schedule_selection(request, proposal, places)
                    except InvalidModelOutput:
                        continue
                    record("restaurant_rescheduled", place_id=closed_id,
                           day_number=day_index + 1)
                    result = proposal
                    rescheduled = True
                    break
                if rescheduled:
                    break
            if rescheduled:
                continue
            if closed.is_required:
                raise
            used = {item.provider_place_id for day in result.days for item in day.items}
            alternatives = sorted(
                (place for place in places if place.category == "식당"
                 and place._restaurant_hours is not None
                 and place.provider_place_id not in used),
                key=lambda place: distance_km(closed, place),
            )
            replaced = False
            for candidate in alternatives:
                proposal = result.model_copy(deep=True)
                for day in proposal.days:
                    for item in day.items:
                        if item.provider_place_id == closed_id:
                            item.provider_place_id = candidate.provider_place_id
                try:
                    validate_places_selection(request, proposal, places)
                    schedule_selection(request, proposal, places)
                except InvalidModelOutput:
                    continue
                record("restaurant_replaced", old_place_id=closed_id,
                       new_place_id=candidate.provider_place_id)
                result = proposal
                replaced = True
                break
            if not replaced:
                proposal = result.model_copy(deep=True)
                for day in proposal.days:
                    day.items = [item for item in day.items
                                 if item.provider_place_id != closed_id]
                try:
                    validate_places_selection(request, proposal, places)
                    schedule_selection(request, proposal, places)
                except InvalidModelOutput:
                    raise exc
                record("restaurant_excluded", place_id=closed_id)
                result = proposal
    raise InvalidModelOutput("restaurant_closed:replacement_exhausted")


async def recommend_places(
    request: ItineraryRequest, places: list[Place], planner: OpenAIPlanner
) -> ModelSelection:
    """관광지·식당을 날짜별로 고르고 보완·시간 조정·검증을 거친다.

    검증 실패는 모델에 한 번 피드백하며 재시도까지 실패하면 원인을 기록한다.
    """
    context = build_context(request, places)
    lodging = lodging_days(request, places)
    # 모델 입력에서 숙소를 제외한다. 먼저 관광·식당 동선을 정한 뒤 주변 숙소를 찾는다.
    context["candidates"] = [
        p for p in context["candidates"] if p["category"] != "숙소"
    ]
    if not context["candidates"]:
        raise GenerationFailed(
            "관광 장소와 식당 후보가 없습니다.", reason="no_place_candidates",
            detail={"region": request.region.full_name, "collected": len(places)},
        )
    allowed = {p["provider_place_id"] for p in context["candidates"]}
    # 필수 방문 순서에서도 숙소를 빼되, 숙소 단계에서는 원래 요청을 그대로 사용한다.
    context["required_order"] = [
        pid for pid in context["required_order"] if pid in allowed
    ]
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    # 숙소에 필요한 시간과 한 자리를 미리 빼고 모델에 남은 범위를 전달한다.
    for index, window in enumerate(context["day_windows"]):
        if index in lodging:
            window["min_items"] = max(0, window["min_items"] - 1)
            window["max_items"] = max(0, window["max_items"] - 1)
            window["available_minutes"] = max(
                0, window["available_minutes"] - policy["숙소"][0] - 20
            )
        # 위에서 숙소 몫은 예약했지만, 이번 모델 응답에는 숙소를 넣지 않도록 알린다.
        window["needs_accommodation"] = False
    feedback = None
    selection = None
    attempts = 2
    # 총 두 번: 최초 생성 한 번 + 검증 오류를 알려 준 뒤 수정 한 번이다.
    for attempt in range(1, attempts + 1):
        selection = None
        try:
            # 모델은 후보 ID와 날짜별 순서를 고른다. 누락된 선택 방문 보완, 시간 초과
            # 축소, 최종 조건 검증은 Python 코드가 수행하므로 모델 응답을 그대로 믿지 않는다.
            selection = await planner.generate(context, feedback, places_only=True)
            selection = complete_selection(request, selection, places, places_only=True)
            selection = trim_places_to_time(request, selection, places)
            validate_places_selection(request, selection, places)
            selection = replace_closed_restaurant(request, selection, places)
            return selection
        except InvalidModelOutput as exc:
            # API 장애 등 모든 오류를 재시도하는 것이 아니라 결과 검증 오류만 수정 요청한다.
            feedback = str(exc)
            record(
                "place_selection_retry", attempt=attempt, reason=feedback,
                pace=request.preference.pace_type,
                days=places_budget_report(request, selection, places),
            )
    # 모델을 더 호출하지 않고 실제 후보를 직접 조합하는 대체 처리로 넘어간다.
    fallback = fallback_short_single_day(request, places)
    if fallback is None:
        fallback = fallback_multi_day(request, places, selection)
    if fallback is not None:
        try:
            fallback = replace_closed_restaurant(request, fallback, places)
        except InvalidModelOutput:
            fallback = None
    if fallback is not None:
        record("place_selection_fallback", days=[
            {"date": day.date, "place_ids": [item.provider_place_id for item in day.items]}
            for day in fallback.days
        ])
        return fallback
    raise GenerationFailed(
        "필수 장소와 여행 시간을 만족하는 장소·식당을 추천하지 못했습니다.",
        reason="places_validation_failed",
        detail={"attempts": attempts, "last_feedback": feedback,
                "candidates": len(context["candidates"]),
                "required_places": len(request.required_places)},
    )


async def recommend_accommodations(
    request: ItineraryRequest,
    selection: ModelSelection,
    places: list[Place],
    client: KakaoPlaces,
) -> tuple[ModelSelection, list[Place]]:
    """각 숙박일에 전체 일정의 시간 제약을 만족하는 숙소를 선택한다.

    반환값은 (숙소 ID까지 포함한 방문 목록, 새 숙소 정보까지 합친 전체 장소 목록)이다.
    이 단계의 시간 검사는 예상 이동시간 기준이며 실제 경로 조회는 다음 단계가 맡는다.
    """
    combined = selection.model_copy(deep=True)
    pool = {p.provider_place_id: p for p in places}
    required = required_hotels_by_day(request, selection, places)
    lodging = lodging_days(request, places)
    for index in sorted(lodging):
        day = combined.days[index]
        day_places = [pool[i.provider_place_id] for i in day.items]
        fixed_hotel = required.get(index)
        next_places = [
            pool[i.provider_place_id]
            for later in combined.days[index + 1 :]
            for i in later.items
        ]
        # 오늘 방문지가 있으면 그 위치들을, 없으면 필수 숙소 또는 이후 첫 방문지를
        # 검색 기준으로 쓴다. 그래도 없으면 확보한 관광·식당 후보 한 곳을 사용한다.
        anchor_places = day_places or (
            [fixed_hotel] if fixed_hotel else next_places[:1]
        )
        if not anchor_places:
            anchor_places = [p for p in places if p.category != "숙소"][:1]
        if not anchor_places:
            raise GenerationFailed("숙소 검색의 기준 위치를 계산할 수 없습니다.", reason="accommodation_anchor_missing", detail={"day_number": index + 1})
        center = Coordinate(
            latitude=sum(p.latitude for p in anchor_places) / len(anchor_places),
            longitude=sum(p.longitude for p in anchor_places) / len(anchor_places),
        )
        # 사용자가 숙소를 지정했다면 다른 숙소로 대체하지 않고 그 숙소만 검사한다.
        if fixed_hotel:
            candidates = [fixed_hotel]
        else:
            candidates = await client.accommodations(
                request, center.latitude, center.longitude
            )
            # 필수 숙소를 지정된 방문 순서보다 앞으로 당기지 않는다.
            candidates = [
                p
                for p in candidates
                if p.provider_place_id
                not in {h.provider_place_id for h in required.values()}
            ]
        next_items = (
            combined.days[index + 1].items if index + 1 < len(combined.days) else []
        )
        anchors = day_places[-1:] + (
            [pool[next_items[0].provider_place_id]] if next_items else []
        )
        # 오늘 마지막 장소 → 숙소 → 내일 첫 장소의 거리가 짧은 후보부터 시험한다.
        # 좌표 거리로 정한 우선순위이며 실제 교통 경로 최적화를 보장하는 것은 아니다.
        candidates.sort(key=lambda p: sum(distance_km(p, anchor) for anchor in anchors))
        chosen, rejections = None, {}
        window = day_windows(request)[index]
        optional_indices = [
            position for position, item in enumerate(day.items)
            if not pool[item.provider_place_id].is_required
        ]
        # 오늘과 다음 날의 선택 방문만 삭제 후보로 둔다. 필수 방문은 제외한다.
        following_day = combined.days[index + 1] if index + 1 < len(combined.days) else None
        following_optional = [
            position for position, item in enumerate(following_day.items)
            if not pool[item.provider_place_id].is_required
        ] if following_day else []

        def preserves_day(items, removed, day_window, reserve):
            # 오늘 방문을 줄일 때 숙소 한 곳을 포함한 최소 개수와 관광·식당 구성을 지킨다.
            if not removed:
                return True
            remaining = [pool[item.provider_place_id] for position, item in enumerate(items)
                         if position not in removed]
            if len(remaining) + reserve < day_window["min_items"]:
                return False
            return (not day_window["needs_tour_and_restaurant"] or
                    {"관광", "식당"} <= {place.category for place in remaining})

        # 숙소 전후 날짜의 선택 방문을 최대 두 곳만 줄인다. 변경이 적은 조합부터
        # 시험하며 필수 방문은 보존하고 최종 검증으로 최소 조건을 확인한다.
        reductions = [((), ())]
        # 첫 조합은 방문을 전혀 삭제하지 않는 경우다. 그것이 실패해야 한 곳, 두 곳
        # 삭제를 시도한다. 각 튜플은 (오늘 삭제할 위치들, 내일 삭제할 위치들)이다.
        for count in (1, 2):
            for current_count in range(count + 1):
                for removed in combinations(optional_indices, current_count):
                    for removed_next in combinations(following_optional, count - current_count):
                        if not preserves_day(day.items, removed, window, 1):
                            continue
                        # 다음 날의 최소 개수는 후보 숙소에서의 예상 이동을 반영해
                        # 마지막 검증에서 판단한다. 필요한 경우 짧은 날을 허용한다.
                        reductions.append((removed, removed_next))
        for removed, removed_next in reductions:
            for candidate in candidates:
                # 실패한 후보의 삭제·추가가 다음 시도에 남지 않도록 매번 복사본을 만든다.
                proposal = combined.model_copy(deep=True)
                proposal.days[index].items = [
                    item for position, item in enumerate(proposal.days[index].items)
                    if position not in removed
                ]
                if following_day:
                    proposal.days[index + 1].items = [
                        item for position, item in enumerate(proposal.days[index + 1].items)
                        if position not in removed_next
                    ]
                proposal.days[index].items.append(
                    SelectionItem(provider_place_id=candidate.provider_place_id)
                )
                # 방문 목록에는 ID만 있으므로 새 숙소의 좌표·분류도 함께 전달해야 한다.
                proposed_pool = {**pool, candidate.provider_place_id: candidate}
                try:
                    # 다음 날 아침 이동도 확인하고, 방문 장소 변경은 최소화한다.
                    generated = schedule_selection(request, proposal, list(proposed_pool.values()))
                    if index == max(lodging):
                        # 앞선 숙박일을 고르는 중에는 이후 숙소가 아직 비어 있다.
                        # 모든 숙박일을 채운 시점에 필수 장소 등 전체 조건까지 검사한다.
                        validate_itinerary(request, generated, list(proposed_pool.values()))
                    chosen = candidate
                    combined, pool = proposal, proposed_pool
                    if removed:
                        record("accommodation_place_trimmed", day_number=index + 1,
                               dropped_place_ids=[day.items[position].provider_place_id
                                                  for position in removed])
                    if removed_next:
                        record("accommodation_next_day_trimmed", day_number=index + 2,
                               dropped_place_ids=[following_day.items[position].provider_place_id
                                                  for position in removed_next])
                    break
                except InvalidModelOutput as exc:
                    # 후보별 실패를 모아 '검색 결과 없음'과 '시간·조건 불일치'를 구분한다.
                    rejections[str(exc)] = rejections.get(str(exc), 0) + 1
            if chosen is not None:
                break
        if chosen is None:
            # 부분 삭제로 해결하지 못한 1박 여행은 날짜별 배분 자체를 다시 시도한다.
            fallback = fallback_two_day_accommodation(
                request, combined, list(pool.values()), candidates,
            ) if index == 0 and len(lodging) == 1 else None
            if fallback is not None:
                combined, fallback_pool = fallback
                pool = {place.provider_place_id: place for place in fallback_pool}
                record("accommodation_selection_fallback", day_number=index + 1,
                       place_ids=[[item.provider_place_id for item in day.items]
                                  for day in combined.days])
                continue
            record("accommodation_unavailable", day_number=index + 1,
                   candidates=len(candidates), rejections=rejections)
            raise GenerationFailed(
                "추천 장소의 동선과 시간을 만족하는 숙소를 찾지 못했습니다.",
                # 후보 0개는 검색 결과 없음, 1개 이상은 모든 후보가 시간 제약 위반이다.
                reason="accommodation_unavailable",
                detail={"day_number": index + 1, "candidates": len(candidates),
                        "rejections": rejections},
            )
    # 경로 단계 전에 전체 일정과 필수 장소를 마지막으로 검사한다.
    try:
        generated = schedule_selection(request, combined, list(pool.values()))
        validate_itinerary(request, generated, list(pool.values()))
    except InvalidModelOutput as exc:
        raise GenerationFailed(
            "추천 장소와 숙소를 여행 시간 안에 배치할 수 없습니다.",
            reason="accommodation_schedule_invalid", detail={"validation": str(exc)},
        ) from exc
    return combined, list(pool.values())


async def connect_routes(
    request: ItineraryRequest,
    selection: ModelSelection,
    places: list[Place],
    model: str,
    router: KakaoRoutes,
) -> RoutesResult:
    """외부 경로 조회의 이동시간으로 시간표를 다시 배치하고 검증한다."""
    try:
        return await schedule_with_routes(request, selection, places, model, router)
    except InvalidModelOutput as exc:
        if str(exc).startswith("restaurant_closed:"):
            closed_id = str(exc).split(":", 1)[1]
            by_id = {place.provider_place_id: place for place in places}
            closed = by_id.get(closed_id)
            if closed is not None and not closed.is_required:
                used = {item.provider_place_id for day in selection.days for item in day.items}
                choices = sorted(
                    (place for place in places if place.category == "식당"
                     and place._restaurant_hours is not None
                     and place.provider_place_id not in used),
                    key=lambda place: distance_km(closed, place),
                )[:6]
                for candidate in [*choices, None]:
                    proposal = selection.model_copy(deep=True)
                    for day in proposal.days:
                        if candidate is None:
                            day.items = [item for item in day.items
                                         if item.provider_place_id != closed_id]
                        else:
                            for item in day.items:
                                if item.provider_place_id == closed_id:
                                    item.provider_place_id = candidate.provider_place_id
                    try:
                        result = await schedule_with_routes(request, proposal, places, model, router)
                    except InvalidModelOutput:
                        continue
                    record("restaurant_route_replanned", old_place_id=closed_id,
                           new_place_id=candidate.provider_place_id if candidate else None)
                    return result
        # 모델/내부 검증 문구를 그대로 노출하는 대신, 사용자가 조정할 수 있는
        # 여행 시간·장소 수를 안내한다. 원래 오류는 detail과 예외 연결에 남긴다.
        raise GenerationFailed(
            "조회한 이동시간과 필수 장소를 여행 시간 안에 배치할 수 없습니다. 여행 시간을 늘리거나 장소를 줄여주세요.",
            # 실제 카카오 이동시간은 장소 선택 단계의 추정치보다 길 수 있다.
            reason="routes_exceed_trip_time", detail={"validation": str(exc)},
        ) from exc



async def generation_stages(
    body: ItineraryRequest,
    client: KakaoPlaces,
    planner: OpenAIPlanner,
    router: KakaoRoutes | None = None,
):
    """최신 생성 로직을 실행하며 SSE와 JSON이 공유하는 단계 이벤트를 내보낸다."""
    # monotonic은 시스템 시계가 바뀌어도 경과 시간 측정에 사용할 수 있는 시계다.
    started = time.monotonic()
    stage_started = started
    stage = "PLACE_RECOMMEND"

    def begin(name: str) -> None:
        # 중첩 함수 밖의 현재 단계와 시작 시각을 갱신해 실패 위치도 기록할 수 있게 한다.
        nonlocal stage, stage_started
        stage, stage_started = name, time.monotonic()
        record("generation_stage", stage=stage, status="STARTED",
               elapsed_ms=round((stage_started - started) * 1000))

    def done() -> None:
        # elapsed_ms는 전체 생성 시작부터, stage_ms는 현재 단계 시작부터 걸린 시간이다.
        now = time.monotonic()
        record("generation_stage", stage=stage, status="COMPLETED",
               elapsed_ms=round((now - started) * 1000),
               stage_ms=round((now - stage_started) * 1000))

    try:
        validate_generation_window(body)  # 외부 API 호출 전에 불가능한 기간을 거른다.
        # 1. 실제 관광·식당 후보 수집 → 후보 정리 → 모델 선택 → 보완 및 검증.
        begin("PLACE_RECOMMEND")
        yield "PLACES", "STARTED", None
        places = select_candidates(
            body, await client.collect(body, include_accommodation=False)
        )
        selection = await recommend_places(body, places, planner)
        done()
        yield "PLACES", "COMPLETED", None

        # 2. 앞서 정한 동선 주변 숙소를 찾고, 숙소를 포함한 방문 목록으로 갱신한다.
        begin("STAY_RECOMMEND")
        yield "ACCOMMODATIONS", "STARTED", None
        selection, places = await recommend_accommodations(
            body, selection, places, client
        )
        done()
        yield "ACCOMMODATIONS", "COMPLETED", None

        # 3. 경로 API에서 이동시간을 조회해 시작·종료 시각이 있는 최종 일정을 만든다.
        begin("ROUTE_OPTIMIZE")
        yield "ROUTES", "STARTED", None
        # 테스트나 호출자가 router를 전달하면 사용하고, 없으면 기본 클라이언트를 만든다.
        router = router or KakaoRoutes(planner.client, planner.settings)
        routes = await connect_routes(
            body, selection, places, planner.settings.openai_model, router
        )
        done()
        yield "ROUTES", "COMPLETED", None

        # 4. 일정 생성에 성공한 뒤 여행용 음악을 추천한다.
        begin("MUSIC_RECOMMEND")
        yield "MUSIC", "STARTED", None
        try:
            music = await planner.recommend_music(body)
        except ApiError as exc:
            # 음악의 API 오류는 여행 일정 전체를 실패시키지 않고 대체곡으로 처리한다.
            # ApiError가 아닌 예외는 아래 공통 오류 처리로 전달된다.
            failure("music_fallback", exc)
            music = fallback_music()
        done()
        yield "MUSIC", "COMPLETED", None
        yield "COMPLETE", "COMPLETED", GenerationResult(itinerary=routes.itinerary, music=music)
    except Exception as exc:
        # 어느 단계에서 실패했는지 남긴 뒤 같은 예외를 다시 올린다.
        # 호출자가 작업 실패 상태와 API 응답을 처리할 수 있도록 오류를 삼키지 않는다.
        failure("pipeline_failed", exc, stage=stage,
                elapsed_ms=round((time.monotonic() - started) * 1000))
        raise


async def generate_plan(
    body: ItineraryRequest,
    client: KakaoPlaces,
    planner: OpenAIPlanner,
    router: KakaoRoutes | None = None,
) -> GenerationResult:
    """SSE와 같은 파이프라인을 끝까지 실행해 JSON용 최종 결과를 반환한다."""
    async with aclosing(generation_stages(body, client, planner, router)) as stages:
        async for stage, _, result in stages:
            if stage == "COMPLETE":
                return result
    raise RuntimeError("Generation finished without a result")
