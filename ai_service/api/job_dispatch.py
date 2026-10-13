"""블로킹 큐 저장소 호출과 기존 매칭 작업 접수 규칙."""
import asyncio
from redis.exceptions import RedisError
from ai_service.jobs.errors import JobApiError
from ai_service.jobs.schemas import MatchingDatabaseJobRequest
from ai_service.matching.mysql import require_mysql_settings
from ai_service.matching.service import eligible_candidates

async def call(request, function, *args):
    service = request.app.state.jobs
    if service is None:
        raise JobApiError(503, 'QUEUE_UNAVAILABLE', '작업 큐 설정이 없습니다.', retryable=True)
    try:
        return await asyncio.to_thread(getattr(service, function), *args)
    except RedisError as exc:
        raise JobApiError(503, 'QUEUE_UNAVAILABLE', '작업 저장소에 연결할 수 없습니다.', retryable=True) from exc

async def submit_matching_job(body, request, settings, idempotency_key=None):
    if isinstance(body, MatchingDatabaseJobRequest):
        require_mysql_settings(settings)
    elif not eligible_candidates(body):
        raise JobApiError(422, 'NO_MATCH_CANDIDATES', '추천할 동행 후보가 없습니다.')
    identity = {'matching_request_id':body.matching_request_id}
    if body.travel_plan_id is not None:
        identity['travel_plan_id']=body.travel_plan_id
    return await call(request, 'submit', 'TRAVELER_MATCHING', body.model_dump(mode='json', by_alias=True, exclude_none=True),
                      identity,
                      request.state.request_id, idempotency_key or f'matching_request_id:{body.matching_request_id}')

