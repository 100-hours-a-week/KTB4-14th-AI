"""V2 동행자 매칭 요청의 Swagger 명세. 실행 API는 백엔드 연동 후 구현한다."""

from copy import deepcopy


MATCHING_REQUEST_EXAMPLE = {
    "preferred_companion_gender": "Female",
    "theme": ["nature", "food"],
    "pace": "Balanced",
    "budget_min": 100000,
    "budget_max": 3000000,
}

MATCHING_RESPONSE_EXAMPLES = {
    "201": {
        "created": {
            "summary": "요청 성공",
            "value": {
                "message": "requests_success",
                "data": {"user_id": 1, **MATCHING_REQUEST_EXAMPLE},
            },
        },
    },
    "400": {
        f"{field}_required": {
            "summary": summary,
            "value": {"message": f"{field}_required", "data": None},
        }
        for field, summary in (
            ("preferred_companion_gender", "동행 성별 선택값 누락 또는 null"),
            ("theme", "여행 테마 선택값 누락 또는 null"),
            ("pace", "여행 속도 선택값 누락 또는 null"),
        )
    },
    "401": {
        "unauthorized": {
            "summary": "로그아웃 또는 인증 만료",
            "value": {"message": "unauthorized", "data": None},
        },
    },
    "500": {
        "server_error": {
            "summary": "서버 내부 오류",
            "value": {"message": "Internal_server_error", "data": None},
        },
    },
}

_REQUEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["preferred_companion_gender", "theme", "pace"],
    "properties": {
        "preferred_companion_gender": {
            "type": "string", "description": "선호하는 동행 성별", "examples": ["Female"],
        },
        "theme": {
            "type": "array", "items": {"type": "string"},
            "description": "선택한 여행 테마", "examples": [["nature", "food"]],
        },
        "pace": {
            "type": "string", "description": "선택한 여행 속도", "examples": ["Balanced"],
        },
        "budget_min": {
            "type": "integer", "description": "최소 예산 (선택)", "examples": [100000],
        },
        "budget_max": {
            "type": "integer", "description": "최대 예산 (선택)", "examples": [3000000],
        },
    },
}

_SCHEMAS = {
    "MatchingRequestCreate": _REQUEST_SCHEMA,
    "MatchingRequestData": {
        "type": "object",
        "additionalProperties": False,
        "required": ["user_id", *_REQUEST_SCHEMA["required"]],
        "properties": {
            "user_id": {
                "type": "integer", "readOnly": True,
                "description": "Authorization 토큰으로 식별한 로그인 사용자 ID",
                "examples": [1],
            },
            **_REQUEST_SCHEMA["properties"],
        },
    },
    "MatchingRequestCreatedResponse": {
        "type": "object",
        "additionalProperties": False,
        "required": ["message", "data"],
        "properties": {
            "message": {"type": "string", "const": "requests_success"},
            "data": {"$ref": "#/components/schemas/MatchingRequestData"},
        },
    },
    "MatchingRequestErrorResponse": {
        "type": "object",
        "additionalProperties": False,
        "required": ["message", "data"],
        "properties": {"message": {"type": "string"}, "data": {"type": "null"}},
    },
}


def add_matching_openapi(schema: dict) -> None:
    """기존 API 스키마에 문서용 계약만 추가하며 실행 라우트는 등록하지 않는다."""
    components = schema.setdefault("components", {})
    components.setdefault("schemas", {}).update(deepcopy(_SCHEMAS))
    tags = schema.setdefault("tags", [])
    if not any(tag["name"] == "V2" for tag in tags):
        tags.append({"name": "V2"})

    descriptions = {
        "201": "매칭 요청 생성 성공",
        "400": "필수 선택값 누락 또는 null. 필드별 오류 예시를 선택하세요.",
        "401": "사용자 인증이 없거나 만료된 경우 (로그아웃 포함)",
        "500": "서버 내부 오류",
    }
    schema["paths"]["/matching-requests"] = {
        "post": {
            "tags": ["V2"],
            "summary": "동행자 후보 추천 작업 생성",
            "operationId": "create_matching_request_v2",
            "x-implementation-status": "planned",
            "security": [{"HTTPBearer": []}],
            "requestBody": {
                "required": True,
                "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/MatchingRequestCreate"},
                    "examples": {"matching": {
                        "summary": "동행자 후보 추천 요청",
                        "value": deepcopy(MATCHING_REQUEST_EXAMPLE),
                    }},
                }},
            },
            "responses": {
                code: {
                    "description": description,
                    "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/" + (
                            "MatchingRequestCreatedResponse" if code == "201"
                            else "MatchingRequestErrorResponse"
                        )},
                        "examples": deepcopy(MATCHING_RESPONSE_EXAMPLES[code]),
                    }},
                }
                for code, description in descriptions.items()
            },
        },
    }
