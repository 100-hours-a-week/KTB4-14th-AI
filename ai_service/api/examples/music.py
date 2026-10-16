"""음악 추천 Swagger 예시."""

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
