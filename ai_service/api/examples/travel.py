"""여행 생성 Swagger 예시. 설명용 값은 실제 여행 안내가 아니다."""
from .music import MUSIC_RESPONSE_EXAMPLES

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

ROUTE_RESPONSE_EXAMPLE = {
    "transport_type": "PUBLIC_TRANSPORT",
    "duration_minutes": 37,
    "distance_meter": 14700,
    "total_fare_amount": 1650,
    "line_name": "9호선 급행",
    "vehicle_number": "201, 211",
    "legs": [
        {
            "mode": "BUS", "line_name": None, "vehicle_number": "201, 211",
            "start": {"name": "출발 정류장", "station_number": None},
            "end": {"name": "환승 정류장", "station_number": None},
        },
        {
            "mode": "WALK", "line_name": None, "vehicle_number": None,
            "start": {"name": "환승 정류장", "station_number": None},
            "end": {"name": "환승역", "station_number": None},
        },
        {
            "mode": "SUBWAY", "line_name": "9호선 급행", "vehicle_number": None,
            "start": {"name": "환승역", "station_number": None},
            "end": {"name": "도착역", "station_number": None},
        },
    ],
}

ITINERARY_RESPONSE_EXAMPLE = {
    "generation_job_id": 10,
    "region": {"region_id": 1, "full_name": "제주특별자치도 서귀포시"},
    "duration": {"arrival_datetime": "2026-09-25T13:00:00", "departure_datetime": "2026-09-25T18:00:00"},
    "headcount": 2, "companion_type": "COUPLE",
    "preference": LEGACY_ITINERARY_REQUEST_EXAMPLE["preference"],
    "required_places": LEGACY_ITINERARY_REQUEST_EXAMPLE["required_places"],
    "title": "성산에서 즐기는 여유로운 제주 여행",
    "days": [{
        "day_number": 1, "travel_date": "2026-09-25",
        "items": [
            {
                "provider": "KAKAO", "provider_place_id": "26338954", "place_name": "성산일출봉",
                "address": "제주특별자치도 서귀포시 성산읍 성산리 1",
                "road_address": "제주특별자치도 서귀포시 성산읍 일출로 284-12",
                "latitude": 33.458056, "longitude": 126.9425, "category": "관광",
                "sequence": 1, "item_type": "TOUR", "start_time": "13:45", "end_time": "15:45",
                "route_from_previous": None,
            },
            {
                "provider": "KAKAO", "provider_place_id": "example-place-2", "place_name": "광치기해변",
                "address": "제주특별자치도 서귀포시 성산읍 고성리", "road_address": "",
                "latitude": 33.452, "longitude": 126.924, "category": "관광",
                "sequence": 2, "item_type": "TOUR", "start_time": "16:00", "end_time": "17:00",
                "route_from_previous": {
                    "transport_type": "PUBLIC_TRANSPORT", "duration_minutes": 15,
                    "distance_meter": 2000, "total_fare_amount": 1650, "line_name": None, "vehicle_number": "201",
                    "legs": [{
                        "mode": "BUS", "vehicle_number": "201", "line_name": None,
                        "start": {"name": "성산일출봉입구", "station_number": None},
                        "end": {"name": "광치기해변", "station_number": None},
                    }],
                },
            },
        ],
    }],
}

ITINERARY_RESPONSE_EXAMPLES = {
    200: {"description": "당일 제주 여행의 성공 응답 구조 예시. 요청 날짜·조건에 따라 실제 결과는 달라집니다. 장소 ID·좌표·노선·시간·요금은 검증된 여행 안내가 아닌 설명용 값입니다. generation_job_id 요청 예시이므로 travel_plan_id는 포함하지 않습니다.",
          "value": ITINERARY_RESPONSE_EXAMPLE},
    400: {"description": "필수 값 누락·잘못된 요청 형식. error_message는 오류 항목에 따라 달라집니다.",
          "value": {"message": "invalid_request", "data": {"error_message": "region: 필수 값이 없습니다."}}},
    401: MUSIC_RESPONSE_EXAMPLES[401],
    422: {"description": "요청은 정상이지만 조건에 맞는 여행 일정을 생성하지 못했습니다. 백엔드 DTO 요청이면 generation_job_id 대신 travel_plan_id를 반환합니다.",
          "value": {"message": "ai_itinerary_generation_failed", "data": {"generation_job_id": 10, "error_message": "추천 가능한 여행 일정을 생성하지 못했습니다."}}},
    500: MUSIC_RESPONSE_EXAMPLES[500],
    503: {"description": "모델·장소 검색·길찾기 서비스 장애, 시간 초과 또는 필수 설정 누락. 길찾기 오류는 routing_service_unavailable로 구분합니다.",
          "value": {"message": "ai_service_unavailable", "data": {"error_message": "잠시 후 다시 시도해주세요."}},
          "alternatives": {"routing": {"summary": "길찾기 서비스 오류", "value": {"message": "routing_service_unavailable", "data": {"error_message": "길찾기 정보를 확인할 수 없습니다. 잠시 후 다시 시도해주세요."}}}}},
}
