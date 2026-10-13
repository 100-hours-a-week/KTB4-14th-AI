"""Swagger 예시·응답 문서를 기존 API 계약에 맞게 등록한다."""
from copy import deepcopy
from fastapi import FastAPI

from ai_service.api.examples import MUSIC_RESPONSE_EXAMPLES, ROUTE_RESPONSE_EXAMPLE
from ai_service.api.responses import ITINERARY_RESPONSES
from ai_service.matching.examples import MATCHING_REQUEST_EXAMPLES, MATCHING_RESPONSE_EXAMPLES


def configure_openapi(app: FastAPI):
    default_openapi = app.openapi

    def openapi_with_response_examples():
        schema = default_openapi()
        responses = schema["paths"]["/internal/ai/music/recommend"]["post"]["responses"]
        # FastAPI가 예시의 data:null을 생략하므로 직렬화 후 원래 응답 예시를 복원한다.
        for code, example in MUSIC_RESPONSE_EXAMPLES.items():
            responses[str(code)]["content"]["application/json"]["example"] = deepcopy(example["value"])
        responses = schema["paths"]["/internal/ai/itineraries/generate"]["post"]["responses"]
        for code, response in ITINERARY_RESPONSES.items():
            responses[str(code)]["content"]["application/json"] = deepcopy(response["content"]["application/json"])
        schema["components"]["schemas"]["RouteSummary"]["examples"] = [deepcopy(ROUTE_RESPONSE_EXAMPLE)]
        # 실제 요청 검증 오류는 공통 핸들러가 400으로 반환한다.
        matching_operation = schema["paths"]["/matching-requests"]["post"]
        matching_operation["responses"].pop("422", None)
        matching_operation["requestBody"]["content"]["application/json"]["examples"] = deepcopy(MATCHING_REQUEST_EXAMPLES)
        for code, examples in MATCHING_RESPONSE_EXAMPLES.items():
            matching_operation["responses"][str(code)]["content"]["application/json"]["examples"] = deepcopy(examples)
        tags = schema.setdefault("tags", [])
        if not any(tag["name"] == "V2" for tag in tags):
            tags.append({"name": "V2"})
        return schema

    app.openapi = openapi_with_response_examples
