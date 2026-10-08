"""Swagger와 로컬 요청에서 동일한 가상 후보 JSON을 사용한다."""

import json
from pathlib import Path


MOCK_MATCHING_REQUEST_PATH = Path(__file__).with_name("data") / "matching_request.mock.json"
MATCHING_RECOMMENDATION_EXAMPLE = json.loads(MOCK_MATCHING_REQUEST_PATH.read_text(encoding="utf-8"))
MATCHING_CREATE_EXAMPLE = {
    "preferred_companion_gender": "Female",
    "theme": ["nature", "food"],
    "pace": "Balanced",
    "budget_min": 100000,
    "budget_max": 3000000,
}

MATCHING_REQUEST_EXAMPLES = {
    "mock": {
        "summary": "자연·맛집 / 여성 동행 / 예산 조건 있음",
        "value": MATCHING_CREATE_EXAMPLE,
    },
    "relaxed": {
        "summary": "바다·휴식 / 성별 무관 / 예산 조건 없음",
        "value": {
            "preferred_companion_gender": "Any",
            "theme": ["beach", "relax"],
            "pace": "Relaxed",
        },
    },
    "empty": {
        "summary": "문화·도심 / 남성 동행 / 예산 조건 있음",
        "value": {
            "preferred_companion_gender": "Male",
            "theme": ["city", "culture"],
            "pace": "Packed",
            "budget_min": 1000000,
            "budget_max": 2000000,
        },
    },
}

# 점수는 응답 형식 설명용이며 실제 E5 결과에 따라 달라진다.
MATCHING_RESPONSE_EXAMPLES = {
    200: {
        "ranked": {
            "summary": "추천 후보 3명 반환",
            "description": "자연·맛집 요청의 응답 형식 예시. 점수는 설명용 값입니다.",
            "value": {
                "message": "ai_companions_recommended",
                "data": {
                    "requester_id": 1,
                    "recommendations": [
                        {"user_id": 2, "score": 0.95, "rank": 1},
                        {"user_id": 7, "score": 0.92, "rank": 2},
                        {"user_id": 3, "score": 0.89, "rank": 3},
                    ],
                },
            },
        },
        "empty": {
            "summary": "후보가 없거나 조건에 맞는 후보 없음",
            "value": {
                "message": "ai_companions_recommended",
                "data": {"requester_id": 1, "recommendations": []},
            },
        },
    },
    400: {
        field: {
            "summary": summary,
            "value": {
                "message": "invalid_request",
                "data": {"error_message": f"{field}: 필수 값이 없습니다."},
            },
        }
        for field, summary in {
            "preferred_companion_gender": "동행 성별 필드 누락",
            "theme": "여행 테마 필드 누락",
            "pace": "여행 속도 필드 누락",
        }.items()
    },
    401: {
        "unauthorized": {
            "summary": "인증 토큰 누락 또는 불일치",
            "value": {"message": "unauthorized", "data": None},
        },
    },
    500: {
        "internal_error": {
            "summary": "예상하지 못한 서버 오류",
            "value": {"message": "internal_server_error", "data": None},
        },
    },
    503: {
        "unavailable": {
            "summary": "E5 모델 로드·추론 실패 또는 시간 초과",
            "value": {
                "message": "ai_service_unavailable",
                "data": {"error_message": "잠시 후 다시 시도해주세요."},
            },
        },
    },
}
