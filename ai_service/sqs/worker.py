"""독립 실행: python -m ai_service.sqs.worker. HTTP API와 분리된 SQS 일정 생성 워커."""
import asyncio
from contextlib import aclosing, suppress
import hashlib
import signal
from uuid import uuid4

import httpx
from pydantic import ValidationError

from ai_service.clients.kakao_places import KakaoPlaces
from ai_service.clients.kakao_routes import KakaoRoutes
from ai_service.core.config import Settings
from ai_service.core.diagnostics import failure, record, request_id as log_request_id
from ai_service.core.errors import ApiError, ServiceUnavailable
from ai_service.planners.travel import OpenAIPlanner
from ai_service.schemas.travel import TravelGenerationRequest
from ai_service.services.travel.backend_contract import STAGE_NAMES, backend_result
from ai_service.services.travel.pipeline import generation_stages
from ai_service.sqs.client import SqsClient, SqsTransportError
from ai_service.sqs.config import SqsSettings
from ai_service.sqs.idempotency import MemoryJobStore, RedisJobStore, LeaseLost, request_fingerprint
from ai_service.sqs.messages import JobMessage, ResultMessage, result_message
from ai_service.sqs.publisher import ResultPublisher


class SqsWorker:
    def __init__(self, client, store, settings: Settings, *, heartbeat_seconds=60):
        self.client, self.store, self.settings = client, store, settings
        self.publisher = ResultPublisher(client)
        self.heartbeat_seconds = heartbeat_seconds

    async def _generate(self, job, current_stage):
        request = TravelGenerationRequest.model_validate(job.request).generation_context()
        if not self.settings.openai_api_key or not self.settings.kakao_rest_api_key:
            raise ServiceUnavailable(reason="provider_keys_missing")
        async with httpx.AsyncClient() as http:
            places = KakaoPlaces(http, self.settings)
            planner = OpenAIPlanner(http, self.settings)
            router = KakaoRoutes(http, self.settings)
            async with aclosing(generation_stages(request, places, planner, router)) as stages:
                async for stage, status, generated in stages:
                    if stage == "COMPLETE":
                        return result_message(
                            job, "COMPLETED", stage="ROUTE_OPTIMIZE", state="DONE",
                            payload=backend_result(generated.model_dump(mode="json")),
                        )
                    current_stage[0] = STAGE_NAMES[stage]
                    await self.publisher.progress(job, stage, status)
        raise RuntimeError("생성 파이프라인에 최종 결과가 없습니다.")

    async def _perform(self, job, receipt, owner):
        current_stage = [None]
        try:
            async with asyncio.timeout(self.settings.generation_timeout_seconds):
                terminal = await self._generate(job, current_stage)
        except (SqsTransportError, LeaseLost):
            # 전송·실행 권한 문제를 여행 생성 FAILED로 확정하지 않는다.
            raise
        except Exception as exc:
            failure("sqs_generation_failed", exc, generation_job_id=job.generation_job_id)
            message = ("일정 생성 요청 형식이 올바르지 않습니다." if isinstance(exc, ValidationError)
                       else "일정 생성 제한 시간을 초과했습니다." if isinstance(exc, TimeoutError)
                       else exc.message if isinstance(exc, ApiError)
                       else "일정 생성 중 내부 오류가 발생했습니다.")
            terminal = result_message(job, "FAILED", stage=current_stage[0],
                                      state="FAILED", error_message=message)
        # 결과를 먼저 기록해 전송 또는 삭제 실패 시 같은 결과를 재발행한다.
        await asyncio.to_thread(self.store.finish, job.generation_job_id, owner, terminal.model_dump_json())
        await self.publisher.publish(terminal)
        await self.client.delete(receipt)

    async def _heartbeat(self, job_id, receipt, owner):
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await asyncio.to_thread(self.store.renew, job_id, owner)
            await self.client.renew_visibility(receipt)

    async def process_message(self, message) -> bool:
        try:
            job = JobMessage.model_validate_json(message["Body"])
            receipt = message["ReceiptHandle"]
            if not receipt:
                raise ValueError("수신 핸들이 없습니다.")
        except (ValidationError, KeyError, ValueError) as exc:
            failure("sqs_message_invalid", exc)
            # 신뢰할 수 있는 작업 식별자가 없으므로 임의 FAILED나 삭제를 하지 않는다.
            return False

        token = log_request_id.set(f"sqs_{job.event_id}")
        owner = uuid4().hex
        tasks = []
        acquired = False
        try:
            claim = await asyncio.to_thread(
                self.store.acquire, job.generation_job_id, request_fingerprint(job.request), owner,
            )
            if claim.state == "BUSY":
                record("sqs_duplicate_running", generation_job_id=job.generation_job_id)
                return False
            if claim.state == "CACHED":
                await self.publisher.publish(ResultMessage.model_validate_json(claim.result))
                await self.client.delete(receipt)
                record("sqs_result_replayed", generation_job_id=job.generation_job_id)
                return True

            acquired = True
            work = asyncio.create_task(self._perform(job, receipt, owner))
            heartbeat = asyncio.create_task(self._heartbeat(job.generation_job_id, receipt, owner))
            tasks = [work, heartbeat]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat  # heartbeat/lease 장애 시 실행 취소, 메시지는 보존.
            await work
            record("sqs_job_finished", generation_job_id=job.generation_job_id)
            return True
        except Exception as exc:
            failure("sqs_job_unacknowledged", exc, generation_job_id=job.generation_job_id)
            return False
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task
            if acquired:
                try:
                    await asyncio.to_thread(self.store.release, job.generation_job_id, owner)
                except Exception as exc:
                    failure("sqs_lease_release_failed", exc, generation_job_id=job.generation_job_id)
            log_request_id.reset(token)

    async def run(self, stop: asyncio.Event):
        while not stop.is_set():
            try:
                messages = await self.client.receive()
                for message in messages:
                    if stop.is_set():
                        break
                    await self.process_message(message)
            except Exception as exc:
                failure("sqs_poll_failed", exc)
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=5)


async def serve():
    settings, sqs_settings = Settings.from_env(), SqsSettings.from_env()
    sqs_settings.require_configured()
    if settings.jobs_redis_url:
        import redis
        store = RedisJobStore(
            redis.Redis.from_url(settings.jobs_redis_url, decode_responses=True,
                                 socket_connect_timeout=3, socket_timeout=3),
            scope=hashlib.sha256(sqs_settings.job_queue_url.encode()).hexdigest()[:16],
            ttl=settings.job_ttl_seconds, lease_seconds=sqs_settings.visibility_timeout,
        )
    else:
        store = MemoryJobStore(ttl=settings.job_ttl_seconds, lease_seconds=sqs_settings.visibility_timeout)
        record("sqs_process_local_dedup", warning_ko="Redis 미설정: 중복 방지는 현재 프로세스에만 적용됩니다.")
    client = SqsClient.from_settings(sqs_settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)
    try:
        await SqsWorker(client, store, settings).run(stop)
    finally:
        await client.close()
        await asyncio.to_thread(store.close)


def main():
    import logging
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(serve())
    except ValueError:
        # URL·비밀값 대신 필요한 설정 이름만 표시한다.
        raise SystemExit("SQS 설정을 확인하세요. AUDIGO_AI_JOB_QUEUE_URL과 AUDIGO_AI_RESULT_QUEUE_URL이 필요합니다.")


if __name__ == "__main__":
    main()
