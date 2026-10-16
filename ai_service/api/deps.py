"""HTTP 요청을 공통 일정 생성 조건으로 바꾸고 실행 설정을 확인한다."""
from typing import Annotated
from fastapi import Header, Request
from ai_service.core.config import Settings
from ai_service.core.errors import ServiceUnavailable
from ai_service.schemas.travel import ItineraryRequest, LegacyGenerationRequest, TravelGenerationRequest
from ai_service.services.travel.transport import resolve_day_transports


def prepare_generation(body: LegacyGenerationRequest | TravelGenerationRequest, request: Request, settings: Settings) -> ItineraryRequest:
    if isinstance(body, LegacyGenerationRequest):
        request.state.generation_job_id = body.generation_job_id
    else:
        request.state.travel_plan_id = body.travel_plan_id
    body = body.generation_context()
    if not settings.openai_api_key or not settings.kakao_rest_api_key:
        raise ServiceUnavailable(reason="provider_keys_missing")
    for mode in set(resolve_day_transports(body).values()):
        request.app.state.routes.require_configured(mode)
    return body

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
