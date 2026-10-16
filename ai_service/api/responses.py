"""공통 오류와 일정 생성의 OpenAPI 응답 정의."""
from ai_service.schemas.common import ErrorResponse
from ai_service.api.examples.travel import ITINERARY_RESPONSE_EXAMPLES

ERROR_RESPONSES = {
    code: {"model": ErrorResponse, "description": description}
    for code, description in {
        400: "필수 값 누락 또는 잘못된 요청 형식",
        401: "인증 토큰이 없거나 유효하지 않음",
        422: "요청 조건 처리 실패",
        500: "서버 내부 오류",
        503: "모델·서비스 장애 또는 처리 시간 초과",
    }.items()
}
ITINERARY_RESPONSES = {
    code: {
        **ERROR_RESPONSES.get(code, {}),
        "description": example["description"],
        "content": {"application/json": {"examples": {
            "default": {"summary": example["value"].get("message", "여행 일정 생성 성공"), "value": example["value"]},
            **example.get("alternatives", {}),
        }}},
    }
    for code, example in ITINERARY_RESPONSE_EXAMPLES.items()
}
