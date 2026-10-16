"""백엔드 동행자 후보 추천 API."""
from typing import Annotated
from fastapi import APIRouter, Body
from ai_service.schemas.matching import BackendMatchingRequest, BackendMatchingResponse
from ai_service.api.examples.matching import MATCHING_REQUEST_EXAMPLES, MATCHING_RESPONSES
from ai_service.services.matching import recommend


def router(settings):
    routes = APIRouter()
    @routes.post('/api/ai/v1/matches/candidates', response_model=BackendMatchingResponse,
        responses=MATCHING_RESPONSES,
        tags=['V2'], summary='백엔드 동행자 후보 추천 (최종 JSON)',
        description='백엔드가 전달한 후보를 테마 일치 최대 80점·속도 일치 최대 20점으로 비교하며, 요청과 후보에 예산이 모두 있으면 예산 불일치로 최대 5점만 감점합니다. 예산이 없으면 감점하지 않습니다. 본인·선호 성별 불일치·RELAXED와 PACKED 조합을 제외합니다. '
                    'limit은 생략 시 3명이며 1~3만 허용합니다. 불완전한 후보는 입력 오류 400으로 거절합니다.')
    def matching_candidates(
        body: Annotated[BackendMatchingRequest,
            Body(openapi_examples=MATCHING_REQUEST_EXAMPLES)],
    ):
        return recommend(body)

    return routes
