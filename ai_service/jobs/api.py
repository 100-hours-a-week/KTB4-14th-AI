import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from ai_service.api_examples import GENERATION_REQUEST_EXAMPLES
from ai_service.auth import require_api_token
from ai_service.features import validate_generation_window
from ai_service.jobs.errors import JobApiError
from ai_service.jobs.schemas import JobAccepted, JobErrorResponse, JobStatus, MatchingJobRequest, MatchingDatabaseJobRequest
from ai_service.jobs.store import RedisJobStore
from ai_service.jobs.worker import create_celery_app, publish
from ai_service.matching import eligible_candidates
from ai_service.matching_backend import BackendMatchingRequest, BackendMatchingResponse, BACKEND_MATCHING_EXAMPLE, recommend_for_backend
from ai_service.matching_mysql import require_mysql_settings
from ai_service.schemas import LegacyGenerationRequest, TravelGenerationRequest
from ai_service.transport import resolve_day_transports

MATCHING_EXAMPLE = {
    'matching_request_id': 201, 'travel_plan_id': 101, 'requester_id': 12,
    'preferred_companion_gender': 'ANY', 'pace': 'BALANCED',
    'budget_min': 300000, 'budget_max': 800000, 'themes': ['자연', '미식'],
    'candidates': [{'user_id': 35, 'pace': 'BALANCED', 'themes': ['자연', '사진'],
                    'budget_min': 300000, 'budget_max': 800000}], 'limit': 10,
}
MATCHING_DB_EXAMPLE = {'matching_request_id': 201, 'requester_id': 201, 'limit': 3}
RESPONSES = {code: {'model': JobErrorResponse} for code in (400, 401, 409, 422, 500, 503)}
logger = logging.getLogger(__name__)


class JobService:
    def __init__(self, store, publisher):
        self.store, self.publisher = store, publisher

    def submit(self, kind, payload, identity, request_id, key):
        doc, created = self.store.reserve(kind, payload, identity, request_id, key)
        # Best-effort low-latency delivery. Redis outbox + beat recovers missed publication.
        if created:
            try:
                score = self.store.client.zscore(self.store.outbox, doc['job_id'])
                self.publisher(doc)
                self.store.dispatched(doc['job_id'], score)
            except Exception as exc:
                logger.warning('queue_delivery_pending job_id=%s error_type=%s', doc['job_id'], type(exc).__name__)
        latest = self.store.get(doc['job_id']) or doc
        return JobAccepted(job_id=doc['job_id'], job_type=doc['job_type'], status=latest['status'],
                           status_url='/api/ai/v1/jobs/' + doc['job_id'])

    def status(self, job_id):
        doc = self.store.get(job_id)
        if not doc:
            raise JobApiError(404, 'JOB_NOT_FOUND', '작업이 없거나 보관 기간이 만료됐습니다.')
        return self.store.public(doc)


def make_job_service(settings):
    if not settings.jobs_redis_url:
        return None
    store = RedisJobStore.from_settings(settings)
    app = create_celery_app(settings)
    return JobService(store, lambda doc: publish(app, doc))


def router(settings):
    routes = APIRouter(prefix='/api/ai/v1', dependencies=[Depends(require_api_token(settings.api_token))])
    key_type = Annotated[str | None, Header(alias='Idempotency-Key', min_length=1, max_length=128)]

    async def call(request, function, *args):
        service = request.app.state.jobs
        if service is None:
            raise JobApiError(503, 'QUEUE_UNAVAILABLE', '작업 큐 설정이 없습니다.', retryable=True)
        try:
            return await asyncio.to_thread(getattr(service, function), *args)
        except RedisError as exc:
            raise JobApiError(503, 'QUEUE_UNAVAILABLE', '작업 저장소에 연결할 수 없습니다.', retryable=True) from exc

    @routes.post('/itinerary-jobs', status_code=202, response_model=JobAccepted,
                 responses=RESPONSES, tags=['V1'], summary='일정 생성 작업 접수 (큐)')
    async def create_itinerary_job(
        body: Annotated[LegacyGenerationRequest | TravelGenerationRequest, Body(openapi_examples=GENERATION_REQUEST_EXAMPLES)],
        request: Request, idempotency_key: key_type = None,
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

    @routes.post('/traveler-match-jobs', status_code=202, response_model=JobAccepted,
                 responses=RESPONSES, tags=['V2'], summary='동행자 추천 작업 접수 (큐)')
    async def create_matching_job(
        body: Annotated[MatchingJobRequest | MatchingDatabaseJobRequest, Body(openapi_examples={
            'database': {'summary': 'MySQL에서 조건·후보 조회', 'value': MATCHING_DB_EXAMPLE},
            'matching': {'summary': '기존 후보 전달 방식', 'value': MATCHING_EXAMPLE},
        })],
        request: Request, idempotency_key: key_type = None,
    ):
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

    @routes.post('/matches/candidates', response_model=BackendMatchingResponse,
        responses={**RESPONSES,202:{'model':JobAccepted,'description':'이전 ID/평면 요청의 큐 접수 응답'}},
        tags=['V2'], summary='백엔드 동행자 후보 추천 (최종 JSON)')
    async def matching_candidates(
        body: Annotated[BackendMatchingRequest | MatchingJobRequest | MatchingDatabaseJobRequest,
            Body(openapi_examples={'backend':{'summary':'백엔드 요청·후보 전달 → 200 최종 결과','value':BACKEND_MATCHING_EXAMPLE},
                                   'database':{'summary':'이전 ID 요청 → 202 작업 접수','value':MATCHING_DB_EXAMPLE}})],
        request: Request, idempotency_key: key_type = None,
    ):
        if isinstance(body,BackendMatchingRequest):
            return await recommend_for_backend(body,request.app.state.backend_matching_recommender,settings)
        # Preserve already integrated ID/flat callers on this URL. Queue
        # clients should use /traveler-match-jobs, whose contract is always 202.
        accepted=await create_matching_job(body,request,idempotency_key)
        return JSONResponse(status_code=202,content=accepted.model_dump(mode='json'))

    @routes.get('/jobs/{job_id}', response_model=JobStatus,
                responses={code: {'model': JobErrorResponse} for code in (400, 401, 404, 500, 503)},
                tags=['jobs'], summary='작업 상태·결과 조회')
    async def get_job(request: Request, job_id: str):
        return await call(request, 'status', job_id)

    return routes
