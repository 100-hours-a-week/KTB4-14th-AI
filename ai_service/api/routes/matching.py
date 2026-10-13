"""백엔드 동행자 후보 추천과 기존 목 후보 추천 API."""
import asyncio
from typing import Annotated
from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from ai_service.api.deps import IdempotencyKey
from ai_service.api.responses import ERROR_RESPONSES
from ai_service.api.job_dispatch import submit_matching_job
from ai_service.core.errors import ServiceUnavailable
from ai_service.jobs.examples import MATCHING_DB_EXAMPLE
from ai_service.jobs.schemas import (
    JobAccepted,
    JobErrorResponse,
    MatchingJobRequest,
    MatchingDatabaseJobRequest,
)
from ai_service.matching.schemas import MatchingRequestCreate, MatchingRecommendationResponse
from ai_service.matching.examples import MATCHING_REQUEST_EXAMPLES, MATCHING_RESPONSE_EXAMPLES
from ai_service.matching.backend import (
    BackendMatchingRequest,
    BackendMatchingResponse,
    BACKEND_MATCHING_EXAMPLE,
    recommend_for_backend,
)

JOB_RESPONSES = {code: {'model': JobErrorResponse} for code in (400, 401, 409, 422, 500, 503)}


def router(settings):
    routes = APIRouter()
    @routes.post(
        "/matching-requests",
        response_model=MatchingRecommendationResponse,
        responses={
            code: {
                **ERROR_RESPONSES.get(code, {}),
                "content": {"application/json": {"examples": examples}},
            }
            for code, examples in MATCHING_RESPONSE_EXAMPLES.items()
        },
        tags=["V2"],
        summary="동행자 후보 추천 (E5)",
        response_description="동행자 후보 추천 성공",
        operation_id="create_matching_request_v2",
    )
    async def recommend_companions(
        body: Annotated[MatchingRequestCreate, Body(openapi_examples=MATCHING_REQUEST_EXAMPLES)],
        request: Request,
    ):
        try:
            async with asyncio.timeout(settings.model_timeout_seconds):
                recommendation = await request.app.state.matching_candidate_source.recommendation_request(body)
                return await request.app.state.matching_recommender.recommend(recommendation)
        except TimeoutError as exc:
            raise ServiceUnavailable(reason="matching_timeout") from exc

    @routes.post('/api/ai/v1/matches/candidates', response_model=BackendMatchingResponse,
        responses={**JOB_RESPONSES,202:{'model':JobAccepted,'description':'이전 ID/평면 요청의 큐 접수 응답'}},
        tags=['V2'], summary='백엔드 동행자 후보 추천 (최종 JSON)')
    async def matching_candidates(
        body: Annotated[BackendMatchingRequest | MatchingJobRequest | MatchingDatabaseJobRequest,
            Body(openapi_examples={'backend':{'summary':'백엔드 요청·후보 전달 → 200 최종 결과','value':BACKEND_MATCHING_EXAMPLE},
                                   'database':{'summary':'이전 ID 요청 → 202 작업 접수','value':MATCHING_DB_EXAMPLE}})],
        request: Request, idempotency_key: IdempotencyKey = None,
    ):
        if isinstance(body,BackendMatchingRequest):
            return await recommend_for_backend(body,request.app.state.backend_matching_recommender,settings)
        # Preserve already integrated ID/flat callers on this URL. Queue
        # clients should use /traveler-match-jobs, whose contract is always 202.
        accepted=await submit_matching_job(body,request,settings,idempotency_key)
        return JSONResponse(status_code=202,content=accepted.model_dump(mode='json'))

    return routes
