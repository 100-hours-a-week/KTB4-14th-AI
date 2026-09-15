from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

import httpx

from ai_service.config import Settings
from ai_service.errors import FeatureNotConfigured


PIPELINE_VERSION = "audigo-ai-pipeline-0.1.0"


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
            "reason": "데모 모드 결과이며 실제 추천이 아닙니다.",
        }
        for index, place_id in enumerate(place_ids, start=1)
    ]
    return (
        {"days": [{"date": trip["start_date"], "stops": stops, "route_segments": []}]},
        "audigo-demo-itinerary-0.1.0",
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
                                "reason": {"type": "string"},
                            },
                            "required": [
                                "order", "provider", "provider_place_id", "name",
                                "latitude", "longitude", "arrival_at", "departure_at",
                                "recommended_stay_minutes", "reason",
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
        "다음 여행 요청으로 일정 JSON을 작성하세요. 제공되지 않은 장소 ID를 실제 제공자 "
        "ID처럼 꾸미지 말고, 확인할 수 없는 값은 빈 일정으로 두세요. 요청:\n"
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
    return json.loads(raw), body.get("modelVersion", settings.gemini_itinerary_model)


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
        key=lambda c: (len(genres & c["genres"]) + len(moods & c["moods"]), c["provider_track_id"]),
    )
    track = {k: v for k, v in winner.items() if k not in {"genres", "moods"}}
    return {
        "track": track,
        "reason": "선택한 장르와 여행 분위기가 가장 많이 일치하는 곡입니다.",
        "model_version": "audigo-music-rule-baseline-0.1.0",
    }


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
    matches = [{**item, "rank": index} for index, item in enumerate(ranked[: payload["limit"]], 1)]
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

