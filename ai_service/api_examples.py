"""Copyable examples matching feature-travel's backend AI request DTO."""

ITINERARY_REQUEST_EXAMPLE = {
    "travel_plan_id": 10,
    "region_id": 1,
    "region_name": "제주특별자치도 서귀포시",
    "arrival_datetime": "2026-08-27T13:00:00",
    "departure_datetime": "2026-08-29T18:00:00",
    "headcount": 2,
    "companion_type": "COUPLE",
    "preference": {
        "pace_type": "RELAXED", "transport_type": "WALK",
        "budget_min": 300000, "budget_max": 800000, "budget_type": "KRW",
        "distance_preference": 70, "themes": ["NATURE", "FOOD"],
        "foods": ["KOREAN", "JAPANESE"], "extra_request": "너무 빡빡하지 않게 추천해주세요.",
    },
    "required_places": [{
        "provider": "KAKAO", "provider_place_id": "26338954", "place_name": "성산일출봉",
        "address": "제주특별자치도 서귀포시 성산읍 성산리 1",
        "latitude": 33.458056, "longitude": 126.9425, "place_type": "TOURISM", "order": 1,
    }],
}

MUSIC_REQUEST_EXAMPLE = {
    "travel_plan_id": 1,
    "region": {"region_id": 1, "full_name": "제주특별자치도 서귀포시"},
    "duration": {"arrival_datetime": "2026-08-27T13:00:00", "departure_datetime": "2026-08-29T18:00:00"},
    "preference": {"themes": ["NATURE", "FOOD"]},
}

MUSIC_RESPONSE_EXAMPLES = {
    200: {
        "description": "여행 정보에 맞는 음악 한 곡과 실제 YouTube 영상 링크를 반환한다. 곡은 응답 형식 예시이며 고정 추천값이 아니다.",
        "value": {"message": "ai_music_recommended", "data": {
            "travel_plan_id": 1, "title": "Yellow", "artist": "Coldplay",
            "youtube_url": "https://www.youtube.com/watch?v=tdVAqxNLXiw",
        }},
    },
    400: {
        "description": "필수 값 누락, 자료형·허용 범위 오류, 미지원 필드 또는 잘못된 JSON. error_message는 발생한 오류에 따라 달라진다.",
        "value": {"message": "invalid_request", "data": {"error_message": "travel_plan_id: 필수 값이 없습니다."}},
    },
    401: {
        "description": "Authorization Bearer 토큰이 없거나 유효하지 않다.",
        "value": {"message": "unauthorized", "data": None},
    },
    422: {
        "description": "여행 정보는 정상적으로 전달됐지만 음악 추천 결과를 생성하지 못한 경우. 재추천 후에도 곡에 맞는 YouTube 영상을 확인하지 못한 경우를 포함한다.",
        "value": {"message": "ai_music_recommendation_failed", "data": {
            "travel_plan_id": 1, "error_message": "추천 가능한 음악을 선택하지 못했습니다.",
        }},
    },
    500: {
        "description": "AI 서버 내부에서 예상하지 못한 오류가 발생했다.",
        "value": {"message": "internal_server_error", "data": None},
    },
    503: {
        "description": "모델·YouTube 조회 장애, 호출 제한, 시간 초과 또는 서버 필수 설정 누락으로 일시적으로 처리할 수 없다.",
        "value": {"message": "ai_service_unavailable", "data": {"error_message": "잠시 후 다시 시도해주세요."}},
    },
}

# Nested request supplied by the user; IDs retain their original meaning.
LEGACY_ITINERARY_REQUEST_EXAMPLE = {
    "generation_job_id": 10,
    "region": {"region_id": 1, "full_name": "제주특별자치도 서귀포시"},
    "duration": {"arrival_datetime": "2026-08-27T13:00:00", "departure_datetime": "2026-08-29T18:00:00"},
    "headcount": 2,
    "companion_type": "COUPLE",
    "preference": {
        "pace_type": "RELAXED", "transport_type": "PUBLIC_TRANSPORT",
        "budget_min": 300000, "budget_max": 800000, "budget_type": "KRW",
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

GENERATION_REQUEST_EXAMPLES = {
    "nested": {"summary": "사용자 요청: generation_job_id / region / duration", "value": LEGACY_ITINERARY_REQUEST_EXAMPLE},
}


# Illustrative route only; numbers/names are not a live journey quotation.
ROUTE_RESPONSE_EXAMPLE = {
    "transport_type": "PUBLIC_TRANSPORT",
    "duration_minutes": 37,
    "distance_meter": 14700,
    "total_fare_amount": 1650,
    "legs": [
        {
            "sequence": 1, "mode": "BUS",
            "boarding_stop": {"name": "출발 정류장", "station_number": None, "vehicle_number": ["201", "211"]},
            "alighting_stop": {"name": "환승 정류장", "station_number": None, "vehicle_number": ["201", "211"]},
            "vehicle_number": ["201", "211"], "line_name": [],
            "duration_minute": 20, "distance_meter": 5000,
        },
        {
            "sequence": 2, "mode": "WALK",
            "boarding_stop": {"name": "환승 정류장", "station_number": None, "vehicle_number": []},
            "alighting_stop": {"name": "환승역", "station_number": None, "vehicle_number": []},
            "vehicle_number": [], "line_name": [],
            "duration_minute": 2, "distance_meter": 200,
        },
        {
            "sequence": 3, "mode": "SUBWAY",
            "boarding_stop": {"name": "환승역", "station_number": None, "vehicle_number": []},
            "alighting_stop": {"name": "도착역", "station_number": None, "vehicle_number": []},
            "vehicle_number": [], "line_name": ["9호선 급행"],
            "duration_minute": 15, "distance_meter": 9500,
        },
    ],
}
