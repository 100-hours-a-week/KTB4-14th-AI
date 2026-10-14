"""큐 작업 접수와 상태·결과 조회 API."""
from typing import Annotated
from fastapi import APIRouter, Body, Request

from ai_service.api.deps import IdempotencyKey
from ai_service.api.examples import GENERATION_REQUEST_EXAMPLES
from ai_service.api.job_dispatch import call
from ai_service.jobs.errors import JobApiError
from ai_service.jobs.schemas import (
    JobAccepted,
    JobErrorResponse,
    JobStatus,
)
from ai_service.travel.schemas import LegacyGenerationRequest, TravelGenerationRequest
from ai_service.travel.features import validate_generation_window
from ai_service.travel.transport import resolve_day_transports

RESPONSES = {code: {'model': JobErrorResponse} for code in (400, 401, 409, 422, 500, 503)}


def router(settings):
    routes = APIRouter(prefix='/api/ai/v1')
    @routes.post('/itinerary-jobs', status_code=202, response_model=JobAccepted,
                 responses=RESPONSES, tags=['V1'], summary='일정 생성 작업 접수 (큐)')
    async def create_itinerary_job(
        body: Annotated[LegacyGenerationRequest | TravelGenerationRequest, Body(openapi_examples=GENERATION_REQUEST_EXAMPLES)],
        request: Request, idempotency_key: IdempotencyKey = None,
    ):
        internal = body.generation_context()
        validate_generation_window(internal)
        resolve_day_transports(internal)
        if not settings.openai_api_key or not settings.kakao_rest_api_key:
            raise JobApiError(503, 'PROVIDER_KEYS_MISSING', '일정 생성 서비스 설정이 없습니다.')
        identity = ({'generation_job_id': internal.generation_job_id} if internal.generation_job_id is not None
                    else {'travel_plan_id': internal.travel_plan_id})
        key = idempotency_key or ':'.join(f'{name}:{value}' for name, value in identity.items())
        return await call(request, 'submit', 'ITINERARY_GENERATION', internal.model_dump(mode='json'),
                          identity, request.state.request_id, key)

    @routes.get('/jobs/{job_id}', response_model=JobStatus,
                responses={code: {'model': JobErrorResponse} for code in (400, 401, 404, 500, 503)},
                tags=['jobs'], summary='작업 상태·결과 조회')
    async def get_job(request: Request, job_id: str):
        return await call(request, 'status', job_id)

    return routes
