"""Wire adapter for feature-travel's existing SSE parser and persistence service."""
import json

from ai_service.streaming import encode_event, stream_generation


STAGE_NAMES = {
    "PLACES": "PLACE_RECOMMEND",
    "ACCOMMODATIONS": "STAY_RECOMMEND",
    "ROUTES": "ROUTE_OPTIMIZE",
    "MUSIC": "MUSIC_RECOMMEND",
}


def backend_result(generated: dict) -> dict:
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
                # The backend cannot express a cross-day endpoint in PendingRoute.
                # Preserve the data without falsely connecting two items in this day.
                stop["route_from_previous"] = route
            items.append(stop)
            previous = item
        days.append({"day_number": day["day_number"], "travel_date": day["travel_date"], "items": items, "routes": routes})
    return {"title": itinerary["title"], "days": days, "music": generated["music"]}


async def stream_backend_generation(body, places, planner, settings, request_id, router=None):
    """Keep results single-copy; release ROUTE DONE only after music succeeds.

    feature-travel saves immediately on ROUTE_OPTIMIZE DONE. Delaying that event
    prevents a later music failure from following a prematurely committed success.
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
