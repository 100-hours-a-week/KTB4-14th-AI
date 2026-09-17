from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable

import httpx

from ai_service.config import Settings
from ai_service.errors import FeatureNotConfigured


PIPELINE_VERSION = "audigo-ai-pipeline-0.2.0"
BLOCKED_RESPONSE_KEYS = {"reason", "source_category"}
StageEmitter = Callable[[str, int, dict[str, Any]], None]


def sanitize_public_result(value: Any) -> Any:
    """Remove fields that must never be exposed by the public API."""
    if isinstance(value, dict):
        return {
            key: sanitize_public_result(item)
            for key, item in value.items()
            if key not in BLOCKED_RESPONSE_KEYS
        }
    if isinstance(value, list):
        return [sanitize_public_result(item) for item in value]
    return value


def _demo_itinerary(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    trip = payload["trip"]
    constraints = payload.get("constraints") or {}
    place_ids = constraints.get("must_visit_place_ids") or []
    stops = [
        {
            "order": index,
            "provider": "REQUEST_INPUT",
            "provider_place_id": place_id,
            "name": place_id,
            "latitude": 0.0,
            "longitude": 0.0,
            "arrival_at": trip["daily_start_time"],
            "departure_at": trip["daily_end_time"],
            "recommended_stay_minutes": 60,
        }
        for index, place_id in enumerate(place_ids, start=1)
    ]
    return (
        {"days": [{"date": trip["start_date"], "stops": stops, "route_segments": []}]},
        "audigo-demo-itinerary-0.2.0",
    )


ITINERARY_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "stops": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "order": {"type": "integer"},
                                "provider": {"type": "string"},
                                "provider_place_id": {"type": "string"},
                                "name": {"type": "string"},
                                "latitude": {"type": "number"},
                                "longitude": {"type": "number"},
                                "arrival_at": {"type": "string"},
                                "departure_at": {"type": "string"},
                                "recommended_stay_minutes": {"type": "integer"},
                            },
                            "required": [
                                "order",
                                "provider",
                                "provider_place_id",
                                "name",
                                "latitude",
                                "longitude",
                                "arrival_at",
                                "departure_at",
                                "recommended_stay_minutes",
                            ],
                        },
                    },
                    "route_segments": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["date", "stops", "route_segments"],
            },
        }
    },
    "required": ["days"],
}


def generate_itinerary(
    payload: dict[str, Any], settings: Settings
) -> tuple[dict[str, Any], str]:
    if settings.mode == "demo":
        return _demo_itinerary(payload)
    if not settings.gemini_api_key:
        raise FeatureNotConfigured("GEMINI_API_KEY가 설정되지 않았습니다.")

    prompt = (
        "다음 여행 요청으로 장소와 식당을 포함한 일정 JSON을 작성하세요. "
        "외부 응답에는 reason과 source_category를 포함하지 마세요. "
        "제공되지 않은 장소 ID를 실제 제공자 ID처럼 꾸미지 말고, "
        "확인할 수 없는 값은 빈 일정으로 두세요. 요청:\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )
    request_body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": ITINERARY_RESPONSE_SCHEMA,
            "temperature": 0.2,
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_itinerary_model}:generateContent"
    )
    with httpx.Client(timeout=45.0) as client:
        response = client.post(
            url,
            headers={"x-goog-api-key": settings.gemini_api_key},
            json=request_body,
        )
        response.raise_for_status()
    body = response.json()
    raw = body["candidates"][0]["content"]["parts"][0]["text"]
    return sanitize_public_result(json.loads(raw)), body.get(
        "modelVersion", settings.gemini_itinerary_model
    )


MUSIC_CATALOG = [
    {
        "provider": "AUDIGO_BASELINE_CATALOG",
        "provider_track_id": "demo_track_001",
        "title": "바다로 가는 길",
        "artist": "Audigo Demo",
        "preview_url": None,
        "genres": {"INDIE", "POP"},
        "moods": {"청량한", "드라이브"},
    },
    {
        "provider": "AUDIGO_BASELINE_CATALOG",
        "provider_track_id": "demo_track_002",
        "title": "느린 산책",
        "artist": "Audigo Demo",
        "preview_url": None,
        "genres": {"ACOUSTIC"},
        "moods": {"차분한", "휴식"},
    },
]


def recommend_music(payload: dict[str, Any]) -> dict[str, Any]:
    prefs = payload["music_preferences"]
    excluded = set(prefs.get("excluded_track_ids", []))
    genres = set(prefs["genres"])
    moods = set(payload["trip_context"].get("mood_tags", []))
    candidates = [c for c in MUSIC_CATALOG if c["provider_track_id"] not in excluded]
    if not candidates:
        raise ValueError("NO_MUSIC_CANDIDATES")
    winner = max(
        candidates,
        key=lambda c: (
            len(genres & c["genres"]) + len(moods & c["moods"]),
            c["provider_track_id"],
        ),
    )
    track = {k: v for k, v in winner.items() if k not in {"genres", "moods"}}
    return {
        "track": track,
        "model_version": "audigo-music-rule-baseline-0.2.0",
    }


def _calculate_accommodation_location(
    itinerary: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    coordinates: list[tuple[float, float]] = []
    for day in itinerary.get("days", []):
        for stop in day.get("stops", []):
            latitude = stop.get("latitude")
            longitude = stop.get("longitude")
            if not isinstance(latitude, (int, float)) or not isinstance(
                longitude, (int, float)
            ):
                continue
            if latitude == 0 and longitude == 0:
                continue
            coordinates.append((float(latitude), float(longitude)))

    latitude = None
    longitude = None
    if coordinates:
        latitude = sum(item[0] for item in coordinates) / len(coordinates)
        longitude = sum(item[1] for item in coordinates) / len(coordinates)

    destination_name = payload["trip"]["destination"]["name"]
    return {
        "area_name": f"{destination_name} 일정 중심 숙소 권역",
        "latitude": latitude,
        "longitude": longitude,
    }


def _connect_routes(
    itinerary: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    transport_modes = payload.get("preferences", {}).get("transport_modes") or []
    transport_mode = transport_modes[0] if transport_modes else "UNSPECIFIED"

    for day in itinerary.get("days", []):
        stops = sorted(day.get("stops", []), key=lambda stop: stop.get("order", 0))
        route_segments = []
        for index, (start, end) in enumerate(zip(stops, stops[1:]), start=1):
            route_segments.append(
                {
                    "order": index,
                    "from_provider_place_id": start.get("provider_place_id"),
                    "from_name": start.get("name"),
                    "to_provider_place_id": end.get("provider_place_id"),
                    "to_name": end.get("name"),
                    "transport_mode": transport_mode,
                }
            )
        day["route_segments"] = route_segments
    return itinerary


def _music_payload_from_trip(payload: dict[str, Any]) -> dict[str, Any]:
    preferences = payload.get("preferences", {})
    pace = str(preferences.get("pace", "")).upper()
    themes = [str(theme) for theme in preferences.get("themes", [])]
    if pace in {"RELAXED", "SLOW"}:
        genres = ["ACOUSTIC"]
        mood_tags = ["차분한", "휴식", *themes]
    else:
        genres = ["INDIE", "POP"]
        mood_tags = ["청량한", "드라이브", *themes]
    return {
        "trip_context": {
            "destination": payload["trip"]["destination"]["name"],
            "season": "AUTO",
            "mood_tags": mood_tags,
        },
        "music_preferences": {
            "genres": genres,
            "excluded_track_ids": [],
        },
    }


def generate_trip_pipeline(
    payload: dict[str, Any],
    settings: Settings,
    emit: StageEmitter,
) -> tuple[dict[str, Any], str]:
    itinerary, itinerary_model_version = generate_itinerary(payload, settings)
    itinerary = sanitize_public_result(itinerary)
    emit(
        "PLACES_AND_RESTAURANTS",
        25,
        {"days": itinerary.get("days", [])},
    )

    accommodation = sanitize_public_result(
        _calculate_accommodation_location(itinerary, payload)
    )
    emit(
        "ACCOMMODATION_LOCATION",
        50,
        {"accommodation": accommodation},
    )

    itinerary = sanitize_public_result(_connect_routes(itinerary, payload))
    emit(
        "ROUTE_CONNECTION",
        75,
        {"days": itinerary.get("days", [])},
    )

    music = sanitize_public_result(recommend_music(_music_payload_from_trip(payload)))
    emit(
        "TRAVEL_MUSIC",
        90,
        {"music": music},
    )

    result = sanitize_public_result(
        {
            **itinerary,
            "accommodation": accommodation,
            "music": music,
        }
    )
    return result, f"{itinerary_model_version}+{music['model_version']}"


def rank_travelers(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    requested_themes = set(payload["themes"])
    pace = payload["pace"]
    ranked = []
    for candidate in payload["candidates"]:
        overlap = len(requested_themes & set(candidate.get("themes", [])))
        pace_match = candidate.get("pace") == pace
        ranked.append(
            {
                "user_id": candidate["user_id"],
                "theme_overlap": overlap,
                "pace_match": pace_match,
                "score": overlap * 10 + int(pace_match),
            }
        )
    ranked.sort(key=lambda x: (-x["score"], x["user_id"]))
    matches = [
        {**item, "rank": index}
        for index, item in enumerate(ranked[: payload["limit"]], 1)
    ]
    if not matches:
        raise ValueError("NO_MATCH_CANDIDATES")
    return (
        {
            "matching_request_id": payload["matching_request_id"],
            "travel_plan_id": payload["travel_plan_id"],
            "matches": matches,
        },
        "audigo-matching-rule-0.1.0",
    )


def generate_checklist(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    context = payload["trip_context"]
    items = ["신분증 챙기기", "휴대전화 충전기 챙기기"]
    if context["travelers"] > 1:
        items.append("동행자와 예약 내역 공유하기")
    return {
        "travel_plan_id": payload["travel_plan_id"],
        "title": f"{context['destination']} 여행 준비물",
        "items": [
            {
                "content": item,
                "is_completed": False,
                "is_checked": False,
                "sort_order": index,
            }
            for index, item in enumerate(items, 1)
        ],
        "model_version": "audigo-checklist-rule-baseline-0.1.0",
    }


def generate_video(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    raise FeatureNotConfigured(
        "영상 모델과 결과 파일 저장소가 아직 연결되지 않았습니다."
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
