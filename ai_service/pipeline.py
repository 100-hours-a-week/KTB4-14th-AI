"""Staged itinerary generation used by the SSE endpoints.

Flow (generation_stages) and where each stage can fail. Every failure is an
ApiError with a ``reason`` code; search CloudWatch for ``"pipeline_failed"`` and
the request_id, then read ``reason`` / ``detail`` / ``frames``.

0. validate_generation_window  trip too short, too many required places
                                (insufficient_trip_time, too_many_required_places)
1. PLACES          Kakao collects tourist spots/restaurants around the region, then
                   the model picks places per day (recommend_places). Planned
                   against the 12h PLACE_DAY window. Over-full days are trimmed
                   automatically; the model gets one correction retry.
                   (no_place_candidates, places_validation_failed,
                    kakao_places_request_failed, openai_request_failed)
2. ACCOMMODATIONS  For each night, the nearest hotel that keeps the whole trip
                   feasible (recommend_accommodations). Scheduled against the
                   wider SCHEDULE_DAY window.
                   (accommodation_unavailable, accommodation_schedule_invalid)
3. ROUTES          Real Kakao walk/transit routes between consecutive items; the
                   day is rescheduled with the real transfer times (connect_routes).
                   (routes_exceed_trip_time, kakao_route_request_failed,
                    restricted_transport_route_missing)
4. MUSIC           One song verified on YouTube. Never fails the trip: on error a
                   fallback song is used and ``music_fallback`` is logged.
Operational events (INFO): place_collection, place_selection_retry (with per-day
available/needed minutes), place_selection_trimmed, accommodation_region_fallback,
generation_stage (timings).
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
    return PlaceResponse.model_validate(place.model_dump(exclude={"source_category", "is_required"}))


def lodging_days(request: ItineraryRequest, places: list[Place]) -> set[int]:
    windows = day_windows(request)
    days = {i for i, w in enumerate(windows) if w["needs_accommodation"]}
    # A specifically requested hotel may be a day-use visit on a single-day trip.
    if not days and any(p.is_required and p.category == "숙소" for p in places):
        days.add(len(windows) - 1)
    return days


def required_hotels_by_day(
    request: ItineraryRequest,
    selection: ModelSelection,
    places: list[Place],
) -> dict[int, Place]:
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
    """Minimum minutes one day of the PLACES stage needs.

    = minimum stay of every place + estimated transfer between consecutive places
      + (lodging day only) minimum hotel stay + 20 minutes to reach it
      + (day 2+) the morning transfer from last night's area. The hotel is not
        known yet; it is picked near the previous day's last place, so that place
        (``arriving_from``) stands in for it, exactly as the accommodation stage
        checks it. Without this, short departure days passed here and then
        failed every hotel candidate later.
    Both the validator and the automatic trimming use this so they never disagree.
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
    """Drop optional places from days whose minimum schedule exceeds the day window.

    The model often picks too many / too distant places for a short day (e.g. the
    departure day). Instead of failing the whole trip after the retries, remove the
    optional place that saves the most minutes until the day fits. Required places,
    the last tourist spot / restaurant of a day that needs both, and removals that
    would put two restaurants back to back are never touched. If a day still does
    not fit, validate_places_selection reports the exact numbers.
    """
    windows = day_windows(request)
    if [d.date for d in selection.days] != [w["date"] for w in windows]:
        return selection  # The validator explains the date mismatch to the model.
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
    """Per-day numbers for logs: why a day did or did not fit its time window."""
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
    """Check the PLACES-stage output before accommodations are searched.

    Raises InvalidModelOutput with a short English instruction; recommend_places
    sends that text back to the model as correction feedback.
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
        # Fewer places than the pace minimum is fine when time, not choice, is the
        # limit (trim_places_to_time removed them); otherwise ask for more.
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
    """Stage 1 (PLACE_RECOMMEND): the model picks tourist spots/restaurants per day.

    Per attempt: model output -> complete_selection (fill missing categories, cap
    counts) -> trim_places_to_time (fit each day's time window) -> validation.
    A validation failure is sent back to the model once as feedback; after the
    last attempt the trip fails with reason=places_validation_failed and the
    per-day budget numbers are logged.
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
    # The model sees the budget left after reserving the night's hotel slot.
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
            # Do not move a required hotel ahead of its required-order slot.
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
                # Try every night against the whole trip so the following morning
                # also has enough transfer time. Published place selections stay fixed.
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
                # candidates == 0: Kakao returned no lodging near the day's places.
                # candidates > 0: every hotel broke the time window (see rejections).
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
    # Final feasibility/required-place check before emitting the lodging result.
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
            # Real Kakao transfer times are longer than the PLACES-stage estimates.
            reason="routes_exceed_trip_time", detail={"validation": str(exc)},
        ) from exc



async def generation_stages(
    body: ItineraryStreamRequest,
    client: KakaoPlaces,
    planner: OpenAIPlanner,
    router: KakaoRoutes | None = None,
):
    """Each yield is sent before executing the next phase; no database/job store."""
    request = body.itinerary_request()
    validate_generation_window(request)  # Reject impossible windows before provider calls.
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
