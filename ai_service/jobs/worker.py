"""Celery가 기존 AI 서비스를 실행하고 Redis가 실행 권한과 재시도를 관리한다."""
import asyncio
from contextlib import aclosing
import time
from uuid import uuid4

from celery import Celery
import httpx
from redis.exceptions import RedisError

from ai_service.services.travel.backend_contract import backend_result, STAGE_NAMES
from ai_service.core.config import Settings
from ai_service.core.diagnostics import request_id as log_request_id, failure
from ai_service.core.errors import ApiError, ServiceUnavailable
from ai_service.jobs.errors import JobApiError
from ai_service.jobs.store import RedisJobStore
from ai_service.planners.travel import OpenAIPlanner
from ai_service.services.travel.pipeline import generation_stages
from ai_service.clients.kakao_places import KakaoPlaces
from ai_service.clients.kakao_routes import KakaoRoutes
from ai_service.schemas.travel import (
    ItineraryRequest
)

def publish(app, doc):
    # Only a job ID is put in the broker; itinerary input and results live in Redis.
    app.send_task('audigo.run_job', args=[doc['job_id']], task_id=doc['job_id'],
                  queue='audigo_itineraries', retry=False)


def dispatch_pending(app, store):
    published = 0
    for job_id, score in store.due():
        doc = store.get(job_id)
        if not doc:
            store.client.zrem(store.outbox, job_id)
            continue
        try:
            publish(app, doc)
            store.dispatched(job_id, score)
            published += 1
        except Exception as exc:
            # Do not lose the durable outbox entry on publication failure.
            failure('queue_publish_failed',exc,code='QUEUE_PUBLISH_FAILED',reason='queue_publish_failed',
                    request_id=doc['request_id'],job_id=job_id)
    return published


async def run_pipeline(doc, store, owner, settings):
    job_id = doc['job_id']
    if doc['job_type'] != 'ITINERARY_GENERATION':
        raise JobApiError(400, 'UNSUPPORTED_JOB_TYPE', '지원하지 않는 작업 유형입니다.')
    request = ItineraryRequest.model_validate(doc['payload'])
    if not settings.openai_api_key or not settings.kakao_rest_api_key:
        raise ServiceUnavailable(reason='provider_keys_missing')
    async with httpx.AsyncClient() as client:
        places, planner, router = KakaoPlaces(client, settings), OpenAIPlanner(client, settings), KakaoRoutes(client, settings)
        async with aclosing(generation_stages(request, places, planner, router)) as stream:
            async for stage, status, result in stream:
                if stage == 'COMPLETE':
                    return backend_result(result.model_dump(mode='json'))
                await asyncio.to_thread(store.progress, job_id, owner, STAGE_NAMES[stage], status)
    raise RuntimeError('Generation did not produce a result')


def job_error(exc):
    if isinstance(exc, TimeoutError):
        return dict(code='JOB_TIMEOUT', message='작업 처리 제한 시간을 초과했습니다.', retryable=True, details=[])
    if isinstance(exc, ApiError):
        configuration = exc.reason and (exc.reason.endswith('_missing') or exc.reason.endswith('_load_failed'))
        return dict(code=exc.code.upper(), message=exc.message,
                    retryable=exc.status_code == 503 and not configuration, details=[])
    return dict(code='INTERNAL_SERVER_ERROR', message='작업 처리 중 내부 오류가 발생했습니다.', retryable=False, details=[])


def execute_job(job_id, store, settings):
    owner = uuid4().hex
    doc = store.claim(job_id, owner)
    if not doc:
        return  # Running duplicates and terminal redelivery cannot replace the result.
    token = log_request_id.set(doc['request_id'])
    try:
        async def bounded():
            async with asyncio.timeout(settings.generation_timeout_seconds):
                return await run_pipeline(doc, store, owner, settings)
        try:
            result = asyncio.run(bounded())
        except RedisError as exc:
            failure('job_storage_failed',exc,code='QUEUE_UNAVAILABLE',reason='job_storage_unavailable',
                    job_id=job_id,attempt=doc['attempt'])
            raise  # Lease/outbox recovery retries storage outages, without committing false failure.
        except Exception as exc:
            error = job_error(exc)
            can_retry = error['retryable'] and doc['attempt'] < doc['max_attempts']
            delay = min(60, settings.job_retry_delay_seconds * 2 ** (doc['attempt'] - 1))
            store.finish(job_id, owner, 'RETRYING' if can_retry else 'FAILED', error=error,
                         retry_at=time.time() + delay if can_retry else 0)
            failure('job_attempt_failed',exc,code=error['code'],job_id=job_id,
                    attempt=doc['attempt'],max_attempts=doc['max_attempts'],retry_scheduled=can_retry,
                    retry_message_ko=('일시적인 오류로 전체 작업을 다시 실행할 예정입니다.' if can_retry
                                      else '자동 재시도 없이 실패 상태로 종료했습니다. 원인을 수정한 뒤 새 작업을 요청하세요.'))
        else:
            store.finish(job_id, owner, 'COMPLETED', result=result)
    finally:
        log_request_id.reset(token)


def create_celery_app(settings):
    app = Celery('audigo', broker=settings.celery_broker_url or settings.jobs_redis_url
                 or 'redis://127.0.0.1:6379/0')
    app.conf.update(
        task_serializer='json', accept_content=['json'], result_serializer='json',
        task_ignore_result=True, task_acks_late=True, task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True,
        broker_connection_timeout=3, broker_transport_options={
            'visibility_timeout': int(settings.generation_timeout_seconds + 120),
            'socket_connect_timeout': 3, 'socket_timeout': 3,
        },
        task_time_limit=int(settings.generation_timeout_seconds + 30),
        task_default_queue='audigo_control',
        beat_schedule={'dispatch-ai-jobs': {'task': 'audigo.dispatch_jobs', 'schedule': 5.0}},
    )

    @app.task(name='audigo.run_job', shared=False, lazy=False)
    def run_job(job_id):
        store = RedisJobStore.from_settings(settings)
        try:
            execute_job(job_id, store, settings)
        finally:
            store.close()

    @app.task(name='audigo.dispatch_jobs', shared=False, lazy=False)
    def dispatch_jobs():
        store = RedisJobStore.from_settings(settings)
        try:
            return dispatch_pending(app, store)
        finally:
            store.close()

    return app


# celery -A ai_service.jobs.worker:celery_app worker / beat
celery_app = create_celery_app(Settings.from_env())
