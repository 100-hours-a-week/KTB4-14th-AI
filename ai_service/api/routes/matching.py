"""백엔드 동행자 후보 추천 API (동기 E5 추천만 지원)."""
from typing import Annotated

from fastapi import APIRouter, Body, Request

from ai_service.matching.backend import (
    BackendMatchingRequest,
    BackendMatchingResponse,
    BACKEND_MATCHING_EXAMPLE,
    recommend_for_backend,
)


def router(settings):
    routes = APIRouter()

    @routes.post(
        '/api/ai/v1/matches/candidates',
        response_model=BackendMatchingResponse,
        tags=['V2'],
        summary='백엔드 동행자 후보 추천 (최종 JSON)',
    )
    async def matching_candidates(
        body: Annotated[
            BackendMatchingRequest,
            Body(openapi_examples={
                'backend': {
                    'summary': '백엔드 요청·후보 전달 → 200 최종 결과',
                    'value': BACKEND_MATCHING_EXAMPLE,
                },
            }),
        ],
        request: Request,
    ):
        return await recommend_for_backend(
            body, request.app.state.backend_matching_recommender, settings
        )

    return routes
