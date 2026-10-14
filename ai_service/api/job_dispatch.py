"""블로킹 큐 저장소 호출과 기존 매칭 작업 접수 규칙."""
import asyncio
from redis.exceptions import RedisError
from ai_service.jobs.errors import JobApiError

async def call(request, function, *args):
    service = request.app.state.jobs
    if service is None:
        raise JobApiError(503, 'QUEUE_UNAVAILABLE', '작업 큐 설정이 없습니다.', retryable=True)
    try:
        return await asyncio.to_thread(getattr(service, function), *args)
    except RedisError as exc:
        raise JobApiError(503, 'QUEUE_UNAVAILABLE', '작업 저장소에 연결할 수 없습니다.', retryable=True) from exc

