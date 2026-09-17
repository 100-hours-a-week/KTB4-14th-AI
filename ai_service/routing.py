from __future__ import annotations

from datetime import datetime, timedelta
import math
from urllib.parse import urlparse

import httpx

from ai_service.config import Settings
from ai_service.errors import InvalidModelOutput, RoutingUnavailable
from ai_service.places import distance_km
from ai_service.schemas import (
    Coordinate,
    KST,
    ModelItinerary,
    PACE_ALIASES,
    RouteDetails,
    RouteLeg,
    RouteSegment,
    RouteStop,
    RoutesResult,
    RouteVehicle,
    TRANSPORT_ALIASES,
    WalkingInstruction,
)


NO_TRANSIT_MESSAGE = "이용할 수 있는 대중교통이 없습니다"


def number(value) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid numeric value")
    value = float(value)
    if not math.isfinite(value) or value < 0 or not value.is_integer():
        raise ValueError("invalid numeric value")
    return int(value)


def path_points(raw) -> list[Coordinate]:
    return [Coordinate(longitude=p[0], latitude=p[1]) for p in raw["path"]["points"]]


def named_point(place) -> RouteStop:
    return RouteStop(
        name=getattr(place, "place_name", getattr(place, "name", "")),
        latitude=place.latitude,
        longitude=place.longitude,
    )


def map_url(value):
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "map.kakao.com":
        raise ValueError("unexpected map URL")
    return value


def night_travel(start: datetime, end: datetime) -> bool:
    cursor = start
    while cursor <= end:
        if cursor.hour >= 22 or cursor.hour < 6:
            return True
        cursor = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return False


class KakaoRoutes:
    """Official Kakao REST routes; planned-date service is not verified.

    https://developers.kakao.com/docs/ko/kakaomap/rest-api#routing
    The API has no departure-date parameter; never invent service flags.
    """

    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client, self.settings = client, settings

    def require_configured(self, transport: str) -> None:
        if (
            TRANSPORT_ALIASES[transport] in {"PUBLIC_TRANSPORT", "WALK"}
            and not self.settings.kakao_rest_api_key
        ):
            raise RoutingUnavailable()

    async def _get(self, mode, origin, destination):
        if not self.settings.kakao_rest_api_key:
            raise RoutingUnavailable()
        try:
            response = await self.client.get(
                "https://dapi.kakao.com/v2/routing/" + mode,
                headers={
                    "Authorization": "KakaoAK " + self.settings.kakao_rest_api_key
                },
                params={
                    "start_x": origin.longitude,
                    "start_y": origin.latitude,
                    "end_x": destination.longitude,
                    "end_y": destination.latitude,
                    "s_name": named_point(origin).name,
                    "e_name": named_point(destination).name,
                    "input_coord": "WGS84",
                    "output_coord": "WGS84",
                },
                timeout=self.settings.routing_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or "status" not in payload:
                raise ValueError("invalid routing response")
            return payload
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise RoutingUnavailable() from exc

    async def _walk(self, origin, destination, departure, *, fallback=False):
        payload = await self._get("walk", origin, destination)
        if (
            payload["status"] == "SAME_POINT"
            and distance_km(origin, destination) < 0.002
        ):
            return RouteDetails(
                provider="KAKAO",
                transport_type="WALK",
                is_estimated=False,
                duration_minutes=0,
                duration_seconds=0,
                distance_meter=0,
                departure_datetime=departure,
                arrival_datetime=departure,
                transit_available=False if fallback else None,
                walking_fallback=fallback,
                message=NO_TRANSIT_MESSAGE if fallback else None,
            )
        if payload["status"] != "OK":
            raise RoutingUnavailable()
        route = payload["route"]
        properties = route["properties"]
        seconds = number(properties["totalTime"])
        distance = number(properties["totalDistance"])
        instructions, path = [], []
        for raw_leg in route["legs"]:
            for raw in raw_leg["steps"]:
                points = path_points(raw)
                path.extend(points)
                instructions.append(
                    WalkingInstruction(
                        description=raw["properties"]["guidance"],
                        distance_meter=number(raw["properties"]["distance"]),
                        path=points,
                    )
                )
        if distance > 0 and (len(path) < 2 or seconds == 0):
            raise ValueError("missing pedestrian path or duration")
        arrival = departure + timedelta(seconds=seconds)
        return RouteDetails(
            provider="KAKAO",
            transport_type="WALK",
            is_estimated=False,
            departure_datetime=departure,
            arrival_datetime=arrival,
            duration_seconds=seconds,
            duration_minutes=math.ceil(seconds / 60),
            distance_meter=distance,
            transit_available=False if fallback else None,
            walking_fallback=fallback,
            message=NO_TRANSIT_MESSAGE if fallback else None,
            map_url=map_url(properties.get("landingUrl")),
            legs=[
                RouteLeg(
                    mode="WALK",
                    duration_seconds=seconds,
                    distance_meter=distance,
                    start=named_point(origin),
                    end=named_point(destination),
                    departure_datetime=departure,
                    arrival_datetime=arrival,
                    is_night_travel=night_travel(departure, arrival),
                    path=path,
                    instructions=instructions,
                )
            ],
        )

    async def _transit(self, payload, origin, destination, departure):
        options = payload["routes"]
        if not isinstance(options, list) or not options:
            raise ValueError("missing successful transit route")
        option = min(options, key=lambda r: number(r["properties"]["totalTime"]))
        if not option["steps"]:
            raise ValueError("missing transit steps")
        legs, cursor, previous = [], departure, named_point(origin)
        for raw in option["steps"]:
            props, path = raw["properties"], path_points(raw)
            mode = {"WALKING": "WALK", "BUS": "BUS", "SUBWAY": "SUBWAY"}[props["type"]]
            distance, seconds = number(props["distance"]), number(props["time"])
            if len(path) < 2 or (distance > 0 and seconds == 0):
                raise ValueError("missing step path or duration")
            stops = [RouteStop(name=s["name"]) for s in props.get("stops", [])]
            start = RouteStop(
                name=stops[0].name if stops else previous.name, **path[0].model_dump()
            )
            end = RouteStop(
                name=stops[-1].name if stops else "도보 도착 지점",
                **path[-1].model_dump(),
            )
            # Kakao may omit WALKING steps. Resolve access/transfer/egress walking
            # explicitly instead of connecting bus stops with invented straight lines.
            if distance_km(previous, start) > 0.01:
                access = await self._walk(previous, start, cursor)
                legs.extend(access.legs)
                cursor = access.arrival_datetime
            vehicles = [
                RouteVehicle.model_validate(v) for v in props.get("vehicles", [])
            ]
            if mode != "WALK" and not vehicles:
                raise ValueError("missing transit line")
            arrival = cursor + timedelta(seconds=seconds)
            legs.append(
                RouteLeg(
                    mode=mode,
                    duration_seconds=seconds,
                    distance_meter=distance,
                    start=start,
                    end=end,
                    departure_datetime=cursor,
                    arrival_datetime=arrival,
                    bus_number=vehicles[0].name if vehicles and mode == "BUS" else None,
                    route_name=(vehicles[0].type + " " + vehicles[0].name)
                    if vehicles
                    else None,
                    vehicles=vehicles,
                    stops=stops,
                    path=path,
                    instructions=[
                        WalkingInstruction(
                            description=props.get("guidance", ""),
                            distance_meter=distance,
                            path=path,
                        )
                    ]
                    if mode == "WALK"
                    else [],
                    is_night_travel=night_travel(cursor, arrival),
                )
            )
            cursor, previous = arrival, end
        if not any(l.mode in {"BUS", "SUBWAY"} for l in legs):
            raise ValueError("no transit leg in successful transit response")
        if distance_km(previous, destination) > 0.01:
            egress = await self._walk(previous, destination, cursor)
            legs.extend(egress.legs)
        # Keep the provider total if it includes additional waiting time; never
        # discard independently resolved walking time from the final schedule.
        total = max(
            number(option["properties"]["totalTime"]),
            sum(l.duration_seconds for l in legs),
        )
        return RouteDetails(
            provider="KAKAO",
            transport_type="PUBLIC_TRANSPORT",
            is_estimated=False,
            departure_datetime=departure,
            arrival_datetime=departure + timedelta(seconds=total),
            duration_seconds=total,
            duration_minutes=math.ceil(total / 60),
            distance_meter=max(
                number(option["properties"]["totalDistance"]),
                sum(l.distance_meter for l in legs),
            ),
            transit_available=True,
            map_url=map_url(payload.get("properties", {}).get("landingURL")),
            legs=legs,
        )

    async def route(self, origin, destination, departure, transport):
        self.require_configured(transport)
        departure = (
            departure.replace(tzinfo=KST)
            if departure.tzinfo is None
            else departure.astimezone(KST)
        )
        try:
            mode = TRANSPORT_ALIASES[transport]
            if mode == "WALK":
                return await self._walk(origin, destination, departure)
            if mode != "PUBLIC_TRANSPORT":
                raise ValueError("unsupported routing mode")
            payload = await self._get("publictraffic", origin, destination)
            if payload["status"] in {
                "NO_RESULTS",
                "STARTNODES_NULL",
                "ENDNODES_NULL",
                "EQUAL_POINTS",
            }:
                return await self._walk(origin, destination, departure, fallback=True)
            if payload["status"] != "OK":
                raise RoutingUnavailable()
            return await self._transit(payload, origin, destination, departure)
        except (KeyError, ValueError, TypeError, IndexError, AttributeError) as exc:
            raise RoutingUnavailable() from exc


async def schedule_with_routes(
    request, selection, places, model: str, router: KakaoRoutes
) -> RoutesResult:
    from ai_service.features import (
        PACE_POLICIES,
        day_windows,
        make_itinerary_response,
        validate_itinerary,
    )

    policy = PACE_POLICIES[PACE_ALIASES[request.preference.pace_type]]
    windows = day_windows(request)
    by_id = {p.provider_place_id: p for p in places}
    if [d.date for d in selection.days] != [w["date"] for w in windows]:
        raise InvalidModelOutput("include every requested date")
    if any(
        item.provider_place_id not in by_id
        for day in selection.days
        for item in day.items
    ):
        raise InvalidModelOutput("use only provided candidate IDs")
    # Cache only inside this generation, with exact departure time in the key.
    cache = {}
    for minimum_stays in (False, True):
        generated_days, routes, transfers = [], [], {}
        previous = None
        try:
            for day_number, (selected, window) in enumerate(
                zip(selection.days, windows), 1
            ):
                cursor = datetime.fromisoformat(
                    f"{selected.date}T{window['start']}"
                ).replace(tzinfo=KST)
                end = datetime.fromisoformat(
                    f"{selected.date}T{window['end']}"
                ).replace(tzinfo=KST)
                items = []
                for sequence, item in enumerate(selected.items, 1):
                    place = by_id[item.provider_place_id]
                    if previous is not None:
                        previous_day, previous_sequence, origin = previous
                        key = (
                            origin.provider_place_id,
                            place.provider_place_id,
                            cursor.isoformat(),
                        )
                        if key not in cache:
                            cache[key] = await router.route(
                                origin, place, cursor, request.preference.transport_type
                            )
                        route = cache[key]
                        routes.append(
                            RouteSegment(
                                **route.model_dump(),
                                from_day_number=previous_day,
                                to_day_number=day_number,
                                from_sequence=previous_sequence,
                                to_sequence=sequence,
                                from_provider_place_id=origin.provider_place_id,
                                to_provider_place_id=place.provider_place_id,
                                origin=Coordinate(
                                    latitude=origin.latitude, longitude=origin.longitude
                                ),
                                destination=Coordinate(
                                    latitude=place.latitude, longitude=place.longitude
                                ),
                            )
                        )
                        transfers[(day_number, sequence)] = route
                        cursor += timedelta(minutes=route.duration_minutes)
                    stay = (
                        policy[place.category][0]
                        if minimum_stays
                        else sum(policy[place.category]) // 2
                    )
                    # Optional meal slack is skipped on the bounded shorter-stay retry.
                    if not minimum_stays and place.category == "식당":
                        hour = (
                            11
                            if cursor.hour < 11
                            else 17
                            if 14 <= cursor.hour < 17
                            else None
                        )
                        if hour is not None:
                            proposed = cursor.replace(hour=hour, minute=0)
                            remaining_stays = sum(
                                policy[by_id[i.provider_place_id].category][0]
                                for i in selected.items[sequence:]
                            )
                            if (
                                proposed + timedelta(minutes=stay + remaining_stays)
                                <= end
                            ):
                                cursor = proposed
                    if cursor + timedelta(minutes=stay) > end:
                        raise InvalidModelOutput(
                            "verified routes and visits exceed the available trip time"
                        )
                    items.append(
                        {
                            "provider_place_id": place.provider_place_id,
                            "start_time": cursor.strftime("%H:%M"),
                            "stay_minutes": stay,
                        }
                    )
                    cursor += timedelta(minutes=stay)
                    previous = day_number, sequence, place
                generated_days.append({"date": selected.date, "items": items})
            generated = ModelItinerary(title=selection.title, days=generated_days)
            days = validate_itinerary(request, generated, places, routes=transfers)
            itinerary = make_itinerary_response(
                request, generated, days, model, routed=True
            )
            if any(route.walking_fallback for route in routes):
                itinerary.warnings.append(
                    NO_TRANSIT_MESSAGE + ". 해당 구간은 도보 경로로 안내합니다."
                )
            return RoutesResult(itinerary=itinerary, routes=routes)
        except InvalidModelOutput:
            if minimum_stays:
                raise
    raise AssertionError("unreachable")
