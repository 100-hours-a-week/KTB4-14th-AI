"""Copyable request examples matching the spreadsheet supplied by the user."""

ITINERARY_REQUEST_EXAMPLE = {
    "generation_job_id": 10,
    "client_draft_id": 1,
    "region": {"region_id": 1, "full_name": "제주특별자치도 서귀포시"},
    "duration": {"arrival_datetime": "2026-08-27T13:00:00", "departure_datetime": "2026-08-29T18:00:00"},
    "headcount": 2,
    "companion_type": "COUPLE",
    "preference": {
        "pace_type": "RELAXED", "transport_type": "PUBLIC_TRANSPORT",
        "budget_min": 300000, "budget_max": 800000, "budget_currency": "KRW",
        "distance_preference": 70, "themes": ["NATURE", "FOOD"],
        "foods": ["KOREAN", "JAPANESE"], "extra_request": "너무 빡빡하지 않게 추천해주세요.",
    },
    "required_places": [{
        "provider": "KAKAO", "provider_place_id": "26338954", "place_name": "성산일출봉",
        "address": "제주특별자치도 서귀포시 성산읍 성산리 1",
        "road_address": "제주특별자치도 서귀포시 성산읍 일출로 284-12",
        "latitude": 33.458056, "longitude": 126.9425, "category": "관광명소", "order": 1,
    }],
}

MUSIC_REQUEST_EXAMPLE = {
    "travel_plan_id": 1,
    "region": {"region_id": 1, "full_name": "제주특별자치도 서귀포시"},
    "duration": {"arrival_datetime": "2026-08-27T13:00:00", "departure_datetime": "2026-08-29T18:00:00"},
    "preference": {"themes": ["NATURE", "FOOD"]},
    "candidates": [{
        "music_id": 3, "title": "개화", "artist": "LUCY",
        "youtube_url": "https://www.youtube.com/watch?v=example",
    }],
}
