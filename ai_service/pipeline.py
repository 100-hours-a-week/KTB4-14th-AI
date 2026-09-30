"""SSE 일정 생성의 단계별 흐름을 조정한다.

여행 시간 사전 검증 후 장소 선택 → 숙소 선택 → 실제 경로 반영 → 음악 추천 순서로 진행한다.
장소 선택은 12시간 창에서 검증하고 한 번 수정할 수 있다. 숙소와 경로는 더 넓은
일정 창에서 전체 여행 가능 여부를 다시 검사한다. 음악 추천 실패에는 대체곡을 쓴다.
실패 원인은 pipeline_failed 로그의 request_id, reason, detail로 추적한다.
"""
from __future__ import annotations


from ai_service.diagnostics import failure, record
from ai_service.errors import ApiError, GenerationFailed, InvalidModelOutput
from ai_service.features import (
    PACE_POLICIES,
    build_context,
    complete_selection,
    day_windows,
    validate_generation_window,
    schedule_selection,
    select_candidates,
    validate_itinerary,
)
from ai_service.model import OpenAIPlanner
from ai_service.music import fallback_music
from ai_service.places import KakaoPlaces, distance_km, travel_minutes
from ai_service.routing import KakaoRoutes, schedule_with_routes
from ai_service.schemas import (
    Accommodation,
    AccommodationsResult,
    Coordinate,
    GenerationResult,
    ItineraryRequest,
    ItineraryStreamRequest,
    ModelSelection,
    PACE_ALIASES,
    Place,
    PlacesResult,
    PlaceResponse,
    RecommendedDay,
    RecommendedItem,
    RoutesResult,
    SelectionItem,
)


def public_place(place: Place) -> PlaceResponse:
    """후보 선정에만 쓰는 내부 필드를 제외하고 공개 장소 응답을 만든다."""
    return PlaceResponse.model_validate(place.model_dump(exclude={"source_category", "is_required"}))


def lodging_days(request: ItineraryRequest, places: list[Place]) -> set[int]:
    """숙박이 필요한 날짜와 당일 방문용 필수 숙소 날짜를 구한다."""
    windows = day_windows(request)
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
    by_id = {p.provider_place_id: p for p in places}
    order = {p.provider_place_id: p.order for p in request.required_places}
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
        if not eligible:
            raise InvalidModelOutput(
                "move required tourist/restaurant visits to dates that leave room for required hotels in required order"
            )
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
    minutes = reserve * (policy["숙소"][0] + 20)
    previous = arriving_from
    for place in day_places:
        minutes += policy[place.category][0]
        if previous is not None:
            minutes += travel_minutes(previous, place, window["transport_type"])
        previous = place
    return minutes


def trim_places_to_time(
    request: ItineraryRequest, selection: ModelSelection, places: list[Place]
) -> ModelSelection:
    """하루 시간이 부족하면 가장 많은 시간을 절약하는 선택 장소부터 줄인다.

    필수 장소, 필요한 카테고리의 마지막 장소, 식당 연속 방문을 유발하는 삭제는 제외한다.
    그래도 시간이 부족하면 검증 단계에서 필요한 시간을 보고한다.
    """
    windows = day_windows(request)
    if [d.date for d in selection.days] != [w["date"] for w in windows]:
        return selection  # 날짜 불일치는 검증 단계에서 모델에 설명한다.
    by_id = {p.provider_place_id: p for p in places}
    lodging = lodging_days(request, places)
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    result = selection.model_copy(deep=True)
    arriving_from = None
    for index, (day, window) in enumerate(zip(result.days, windows)):
        if any(item.provider_place_id not in by_id for item in day.items):
            continue
        reserve = int(index in lodging)
        chosen = [by_id[item.provider_place_id] for item in day.items]
        needed = {"관광", "식당"} if window["needs_tour_and_restaurant"] else set()

        def minutes(day_places, start=arriving_from):
            return places_day_minutes(day_places, window, reserve, policy, start)

        before = minutes(chosen)
        dropped = []
        while minutes(chosen) > window["available_minutes"]:
            candidates = []
            for i, place in enumerate(chosen):
                if place.is_required:
                    continue
                if place.category in needed and sum(p.category == place.category for p in chosen) == 1:
                    continue
                rest = chosen[:i] + chosen[i + 1 :]
                if any(a.category == b.category == "식당" for a, b in zip(rest, rest[1:])):
                    continue
                candidates.append((minutes(rest), i))
            if not candidates:
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
    return result


def places_budget_report(
    request: ItineraryRequest, selection: ModelSelection | None, places: list[Place]
) -> list[dict]:
    """날짜별 가용 시간과 필요 시간을 로그용으로 요약한다."""
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
    if [d.date for d in selection.days] != [w["date"] for w in windows]:
        raise InvalidModelOutput("include every requested date exactly once in order")
    by_id = {p.provider_place_id: p for p in places}
    lodging = lodging_days(request, places)
    seen = []
    arriving_from = None
    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    for index, (day, window) in enumerate(zip(selection.days, windows)):
        reserve = int(index in lodging)
        minimum, maximum = (
            max(0, window["min_items"] - reserve),
            max(0, window["max_items"] - reserve),
        )
        categories = set()
        day_places = []
        for item in day.items:
            place = by_id.get(item.provider_place_id)
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
        arriving_from = day_places[-1] if day_places else arriving_from
        if len(day.items) > maximum:
            raise InvalidModelOutput(
                f"{day.date}: select {minimum}..{maximum} tourist/restaurant places, leaving room for lodging"
            )
        # 시간 부족으로 자동 축소했다면 최소 장소 수보다 적어도 허용한다.
        cheapest_extra = min(policy["관광"][0], policy["식당"][0]) + 5
        if len(day.items) < minimum and needed_minutes + cheapest_extra <= window["available_minutes"]:
            raise InvalidModelOutput(
                f"{day.date}: select {minimum}..{maximum} tourist/restaurant places, leaving room for lodging"
            )
        if window["needs_tour_and_restaurant"] and not {"관광", "식당"} <= categories:
            raise InvalidModelOutput(
                f"{day.date}: include a tourist place and a restaurant"
            )
        if needed_minutes > window["available_minutes"]:
            raise InvalidModelOutput(
                f"{day.date}: choose fewer/closer places to leave time for accommodation and transfers "
                f"(needs {needed_minutes} min, window has {window['available_minutes']} min)"
            )
    required = [
        p.provider_place_id
        for p in sorted(request.required_places, key=lambda p: p.order)
        if by_id[p.provider_place_id].category != "숙소"
    ]
    if [pid for pid in seen if pid in required] != required:
        raise InvalidModelOutput(
            "include ALL required tourist/restaurant places in required_order"
        )
    required_hotels_by_day(request, selection, places)


async def recommend_places(
    request: ItineraryRequest, places: list[Place], planner: OpenAIPlanner
) -> ModelSelection:
    """관광지·식당을 날짜별로 고르고 보완·시간 조정·검증을 거친다.

    검증 실패는 모델에 한 번 피드백하며 재시도까지 실패하면 원인을 기록한다.
    """
    context = build_context(request, places)
    lodging = lodging_days(request, places)
    context["candidates"] = [
        p for p in context["candidates"] if p["category"] != "숙소"
    ]
    if not context["candidates"]:
        raise GenerationFailed(
            "관광 장소와 식당 후보가 없습니다.", reason="no_place_candidates",
            detail={"region": request.region.full_name, "collected": len(places)},
        )
    allowed = {p["provider_place_id"] for p in context["candidates"]}
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
        window["needs_accommodation"] = False
    feedback = None
    selection = None
    attempts = 2
    for attempt in range(1, attempts + 1):
        selection = None
        try:
            selection = await planner.generate(context, feedback, places_only=True)
            selection = complete_selection(request, selection, places, places_only=True)
            selection = trim_places_to_time(request, selection, places)
            validate_places_selection(request, selection, places)
            return selection
        except InvalidModelOutput as exc:
            feedback = str(exc)
            record(
                "place_selection_retry", attempt=attempt, reason=feedback,
                pace=request.preference.pace_type,
                days=places_budget_report(request, selection, places),
            )
    raise GenerationFailed(
        "필수 장소와 여행 시간을 만족하는 장소·식당을 추천하지 못했습니다.",
        reason="places_validation_failed",
        detail={"attempts": attempts, "last_feedback": feedback,
                "candidates": len(context["candidates"]),
                "required_places": len(request.required_places)},
    )


def places_result(selection: ModelSelection, places: list[Place]) -> PlacesResult:
    by_id = {p.provider_place_id: p for p in places}
    return PlacesResult(
        title=selection.title,
        days=[
            RecommendedDay(
                day_number=index,
                travel_date=day.date,
                items=[
                    RecommendedItem(
                        **public_place(by_id[item.provider_place_id]).model_dump(),
                        sequence=sequence,
                    )
                    for sequence, item in enumerate(day.items, 1)
                ],
            )
            for index, day in enumerate(selection.days, 1)
        ],
    )


async def recommend_accommodations(
    request: ItineraryRequest,
    selection: ModelSelection,
    places: list[Place],
    client: KakaoPlaces,
) -> tuple[ModelSelection, list[Place], AccommodationsResult]:
    """각 숙박일에 전체 일정의 시간 제약을 만족하는 숙소를 선택한다."""
    combined = selection.model_copy(deep=True)
    pool = {p.provider_place_id: p for p in places}
    required = required_hotels_by_day(request, selection, places)
    recommendations = []
    for index in sorted(lodging_days(request, places)):
        day = combined.days[index]
        day_places = [pool[i.provider_place_id] for i in day.items]
        fixed_hotel = required.get(index)
        next_places = [
            pool[i.provider_place_id]
            for later in combined.days[index + 1 :]
            for i in later.items
        ]
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
        candidates.sort(key=lambda p: sum(distance_km(p, anchor) for anchor in anchors))
        chosen, rejections = None, {}
        for candidate in candidates:
            proposal = combined.model_copy(deep=True)
            proposal.days[index].items.append(
                SelectionItem(provider_place_id=candidate.provider_place_id)
            )
            proposed_pool = {**pool, candidate.provider_place_id: candidate}
            try:
                # 다음 날 아침 이동까지 포함해 전체 일정을 검증하며 장소 선택은 유지한다.
                schedule_selection(request, proposal, list(proposed_pool.values()))
                chosen = candidate
                combined, pool = proposal, proposed_pool
                break
            except InvalidModelOutput as exc:
                rejections[str(exc)] = rejections.get(str(exc), 0) + 1
                continue
        if chosen is None:
            record("accommodation_unavailable", day_number=index + 1,
                   candidates=len(candidates), rejections=rejections)
            raise GenerationFailed(
                "추천 장소의 동선과 시간을 만족하는 숙소를 찾지 못했습니다.",
                # 후보 0개는 검색 결과 없음, 1개 이상은 모든 후보가 시간 제약 위반이다.
                reason="accommodation_unavailable",
                detail={"day_number": index + 1, "candidates": len(candidates),
                        "rejections": rejections},
            )
        recommendations.append(
            Accommodation(
                day_number=index + 1,
                travel_date=day.date,
                place=public_place(chosen),
            )
        )
    # 숙소 결과를 보내기 전에 전체 일정과 필수 장소를 마지막으로 검사한다.
    try:
        generated = schedule_selection(request, combined, list(pool.values()))
        validate_itinerary(request, generated, list(pool.values()))
    except InvalidModelOutput as exc:
        raise GenerationFailed(
            "추천 장소와 숙소를 여행 시간 안에 배치할 수 없습니다.",
            reason="accommodation_schedule_invalid", detail={"validation": str(exc)},
        ) from exc
    return (
        combined,
        list(pool.values()),
        AccommodationsResult(accommodations=recommendations),
    )


async def connect_routes(
    request: ItineraryRequest,
    selection: ModelSelection,
    places: list[Place],
    model: str,
    router: KakaoRoutes,
) -> RoutesResult:
    try:
        return await schedule_with_routes(request, selection, places, model, router)
    except InvalidModelOutput as exc:
        raise GenerationFailed(
            "조회한 이동시간과 필수 장소를 여행 시간 안에 배치할 수 없습니다. 여행 시간을 늘리거나 장소를 줄여주세요.",
            # 실제 카카오 이동시간은 장소 선택 단계의 추정치보다 길 수 있다.
            reason="routes_exceed_trip_time", detail={"validation": str(exc)},
        ) from exc



async def generation_stages(
    body: ItineraryStreamRequest,
    client: KakaoPlaces,
    planner: OpenAIPlanner,
    router: KakaoRoutes | None = None,
):
    """단계 시작·완료를 순서대로 내보내며 별도 작업 저장소는 사용하지 않는다."""
    request = body.itinerary_request()
    validate_generation_window(request)  # 외부 API 호출 전에 불가능한 기간을 거른다.
    yield "PLACES", "STARTED", None
    places = select_candidates(
        request, await client.collect(request, include_accommodation=False)
    )
    selection = await recommend_places(request, places, planner)
    yield "PLACES", "COMPLETED", places_result(selection, places)

    yield "ACCOMMODATIONS", "STARTED", None
    selection, places, accommodations = await recommend_accommodations(
        request, selection, places, client
    )
    yield "ACCOMMODATIONS", "COMPLETED", accommodations

    yield "ROUTES", "STARTED", None
    router = router or KakaoRoutes(planner.client, planner.settings)
    routes = await connect_routes(
        request, selection, places, planner.settings.openai_model, router
    )
    yield "ROUTES", "COMPLETED", routes

    yield "MUSIC", "STARTED", None
    try:
        music = await planner.recommend_music(request)
    except ApiError as exc:
        failure("music_fallback", exc)
        music = fallback_music()
    result = GenerationResult(**routes.model_dump(), music=music)
    yield "MUSIC", "COMPLETED", music
    yield "COMPLETE", "COMPLETED", result
