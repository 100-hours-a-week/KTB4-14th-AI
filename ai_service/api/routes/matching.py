"""백엔드 동행자 후보 추천 API."""
from typing import Annotated
from fastapi import APIRouter, Body
from ai_service.jobs.schemas import JobErrorResponse
from ai_service.matching.backend import (
    BackendMatchingRequest,
    BackendMatchingResponse,
    BACKEND_MATCHING_EXAMPLE,
)
from ai_service.matching.service import recommend

RESPONSES = {code: {'model': JobErrorResponse} for code in (400, 401, 500, 503)}


def router(settings):
    routes = APIRouter()
    @routes.post('/api/ai/v1/matching-requests', response_model=BackendMatchingResponse,
        responses=RESPONSES,
        tags=['V2'], summary='백엔드 동행자 후보 추천 (최종 JSON)',
        description='백엔드가 전달한 후보를 테마 일치 최대 80점·속도 일치 최대 20점으로 비교합니다. 본인·선호 성별 불일치·RELAXED와 PACKED 조합을 제외합니다. '
                    'limit은 생략 시 3명이며 1~3만 허용합니다. 불완전한 후보는 입력 오류 400으로 거절합니다.')
    def matching_candidates(
        body: Annotated[BackendMatchingRequest,
            Body(openapi_examples={'backend':{'summary':'백엔드 요청·후보 전달 → 200 최종 결과','value':BACKEND_MATCHING_EXAMPLE}})],
    ):
        return recommend(body)

    return routes
