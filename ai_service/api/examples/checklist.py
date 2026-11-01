"""체크리스트 API의 Swagger 요청·응답 예시."""
from ai_service.schemas.checklist import ChecklistResponse
from ai_service.schemas.common import StructuredErrorResponse


CHECKLIST_REQUEST_EXAMPLE = {
    'travel_plan_id': 101,
    'trip_context': {
        'destination': '부산', 'start_date': '2026-11-10', 'end_date': '2026-11-11',
        'travelers': 2, 'themes': ['NATURE', 'FOOD'],
    },
}
CHECKLIST_RESPONSE_EXAMPLE = {
    'travel_plan_id': 101,
    'title': '부산 바다 여행 준비물',
    'items': [
        {'content': '신분증 챙기기', 'is_completed': False, 'is_checked': False, 'sort_order': 1},
        {'content': '휴대전화 충전기 챙기기', 'is_completed': False, 'is_checked': False, 'sort_order': 2},
    ],
}

CHECKLIST_RESPONSES = {
    200: {
        'model': ChecklistResponse,
        'description': '체크리스트 생성 성공. 백엔드가 결과를 DB에 저장합니다.',
        'content': {'application/json': {'example': CHECKLIST_RESPONSE_EXAMPLE}},
    },
}
for status, code, message, description in (
    (400, 'INVALID_REQUEST', '입력값을 확인해 주세요.', '필수 값 누락, JSON·자료형·날짜·허용 범위 오류'),
    (401, 'UNAUTHORIZED', '인증 정보를 확인해 주세요.', '서비스 Bearer 토큰이 없거나 유효하지 않음'),
    (422, 'CHECKLIST_GENERATION_FAILED', '체크리스트를 생성하지 못했습니다.', '정상 요청에 대해 유효한 생성 결과를 만들지 못함'),
    (500, 'INTERNAL_SERVER_ERROR', '서버 내부 오류가 발생했습니다.', '예상하지 못한 AI 서버 내부 오류'),
    (503, 'AI_SERVICE_UNAVAILABLE', '잠시 후 다시 시도해 주세요.', '모델 장애·호출 제한·시간 초과·필수 설정 누락'),
):
    CHECKLIST_RESPONSES[status] = {
        'model': StructuredErrorResponse,
        'description': description,
        'content': {'application/json': {'example': {
            'error': {
                'code': code, 'message': message, 'retryable': status == 503,
                'details': [{'field': 'body.trip_context.destination', 'type': 'missing'}]
                if status == 400 else [],
            },
            'request_id': 'req_checklist_example',
        }}},
    }
