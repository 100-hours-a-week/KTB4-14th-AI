"""기존 feature-travel의 SSE 파서와 저장 형식에 맞추는 변환 계층."""
import json

from ai_service.streaming import encode_event, stream_generation


STAGE_NAMES = {
    "PLACES": "PLACE_RECOMMEND",
    "ACCOMMODATIONS": "STAY_RECOMMEND",
    "ROUTES": "ROUTE_OPTIMIZE",
    "MUSIC": "MUSIC_RECOMMEND",
}


def backend_result(generated: dict) -> dict:
    """내부 일정의 장소·경로를 백엔드가 저장하는 날짜별 구조로 바꾼다."""
    itinerary = generated["itinerary"]
    days = []
    for day in itinerary["days"]:
        items, routes = [], []
        previous = None
        for item in day["items"]:
            stop = {key: item[key] for key in (
                "provider", "provider_place_id", "place_name", "address",
                "latitude", "longitude", "sequence", "start_time", "end_time",
            )}
            stop["place_type"] = {"TOUR": "TOURISM", "RESTAURANT": "RESTAURANT", "ACCOMMODATION": "ACCOMMODATION"}[item["item_type"]]
            route = item.get("route_from_previous")
            if route is not None and previous is not None:
                routes.append({
                    "from_sequence": previous["sequence"], "to_sequence": item["sequence"],
                    **route, "order": len(routes) + 1,
                })
            elif route is not None:
                # 날짜를 넘는 이동은 백엔드의 PendingRoute로 표현할 수 없어 장소에 보관한다.
                stop["route_from_previous"] = route
            items.append(stop)
            previous = item
        days.append({"day_number": day["day_number"], "travel_date": day["travel_date"], "items": items, "routes": routes})
    return {"title": itinerary["title"], "days": days, "music": generated["music"]}


async def stream_backend_generation(body, places, planner, settings, request_id, router=None):
    """최종 결과를 한 번만 보내고 음악 단계까지 끝난 뒤 경로 완료를 알린다.

    백엔드는 ROUTE_OPTIMIZE_DONE 수신 즉시 저장하므로 완료 이벤트를 늦춘다.
    """
    identity = ({"travel_plan_id": body.travel_plan_id} if body.travel_plan_id is not None
                else {"generation_job_id": body.generation_job_id})
    sequence = 0
    stream = stream_generation(body, places, planner, settings, request_id, router)
    try:
        async for chunk in stream:
            if chunk.startswith(":"):
                yield chunk
                continue
            data = json.loads(chunk.split("data: ", 1)[1])
            stage, status = data["stage"], data["status"]
            if status == "FAILED":
                sequence += 1
                yield encode_event("error", sequence, {
                    **identity,
                    "stage": STAGE_NAMES.get(stage, stage), "status": "FAILED",
                    "message": data["message"], "data": data["data"],
                })
                return
            if stage == "ROUTES" and status == "COMPLETED":
                # 음악 추천이 끝나기 전에는 백엔드의 저장 트리거를 보내지 않는다.
                continue
            if stage == "COMPLETE":
                result = backend_result(data["data"])
                sequence += 1
                yield encode_event("ROUTE_OPTIMIZE_DONE", sequence, {
                    **identity,
                    "stage": "ROUTE_OPTIMIZE", "status": "DONE", "result": result,
                })
                sequence += 1
                yield encode_event("complete", sequence, {
                    **identity, "stage": "COMPLETE", "status": "COMPLETED",
                })
                continue
            name = STAGE_NAMES[stage]
            sequence += 1
            yield encode_event(f"{name}_{'STARTED' if status == 'STARTED' else 'DONE'}", sequence, {
                "stage": name, "status": "RUNNING" if status == "STARTED" else "DONE",
            })
    finally:
        await stream.aclose()
