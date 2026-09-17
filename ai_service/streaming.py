from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging

from ai_service.errors import ApiError, ServiceUnavailable
from ai_service.pipeline import generation_stages


logger = logging.getLogger(__name__)
STAGES = ("PLACES", "ACCOMMODATIONS", "ROUTES", "MUSIC")


def encode_event(name: str, sequence: int, payload: dict) -> str:
    return f"id: {sequence}\nevent: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_generation(
    body, places, planner, settings, request_id: str, router=None
):
    generator = generation_stages(body, places, planner, router)
    pending = None
    sequence = 0
    stage = STAGES[0]
    progress = 0
    deadline = asyncio.get_running_loop().time() + settings.stream_timeout_seconds
    identity = {
        "generation_job_id": body.generation_job_id,
        "request_id": request_id,
    }
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ServiceUnavailable()
            if pending is None:
                pending = asyncio.create_task(anext(generator))
            done, _ = await asyncio.wait(
                {pending}, timeout=min(settings.stream_heartbeat_seconds, remaining)
            )
            # A result arriving after the deadline must not be emitted as success.
            if asyncio.get_running_loop().time() >= deadline:
                raise ServiceUnavailable()
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                stage, status, result = pending.result()
            except StopAsyncIteration:
                break
            finally:
                pending = None
            sequence += 1
            if status == "COMPLETED" and stage in STAGES:
                progress = (STAGES.index(stage) + 1) * 25
            event = (
                "complete"
                if stage == "COMPLETE"
                else "stage_completed"
                if status == "COMPLETED"
                else "stage_started"
            )
            yield encode_event(
                event,
                sequence,
                {
                    **identity,
                    "stage": stage,
                    "status": status,
                    "progress": progress,
                    "data": result.model_dump(mode="json")
                    if result is not None
                    else None,
                },
            )
    except asyncio.CancelledError:
        raise  # Client disconnected; cancellation closes in-flight HTTP requests.
    except Exception as exc:
        if isinstance(exc, ApiError):
            code, http_status, message = exc.code, exc.status_code, exc.message
        else:
            code, http_status, message = (
                "internal_server_error",
                500,
                "서버 내부 오류가 발생했습니다.",
            )
            logger.error(
                "Pipeline failed type=%s request_id=%s", type(exc).__name__, request_id
            )
        # HTTP headers have already been sent: failure is a terminal SSE event.
        yield encode_event(
            "error",
            sequence + 1,
            {
                **identity,
                "stage": stage,
                "status": "FAILED",
                "progress": progress,
                "message": code,
                "data": {"http_status": http_status, "error_message": message},
            },
        )
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pending
        await generator.aclose()
