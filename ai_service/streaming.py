from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import time

from ai_service.errors import ApiError, ServiceUnavailable
from ai_service.pipeline import generation_stages
from ai_service.diagnostics import request_id as log_request_id, record, failure


STAGES = ("PLACES", "ACCOMMODATIONS", "ROUTES", "MUSIC")


def encode_event(name: str, sequence: int, payload: dict) -> str:
    return f"id: {sequence}\nevent: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_generation(
    body, places, planner, settings, request_id: str, router=None
):
    token = log_request_id.set(request_id)
    started = time.monotonic()
    stage_started = started
    generator = generation_stages(body, places, planner, router)
    pending = None
    sequence = 0
    stage = STAGES[0]
    deadline = asyncio.get_running_loop().time() + settings.stream_timeout_seconds
    identity = {
        "generation_job_id": body.generation_job_id,
    }
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ServiceUnavailable(reason="stream_timeout", detail={"timeout_seconds": settings.stream_timeout_seconds})
            if pending is None:
                pending = asyncio.create_task(anext(generator))
            done, _ = await asyncio.wait(
                {pending}, timeout=min(settings.stream_heartbeat_seconds, remaining)
            )
            # A result arriving after the deadline must not be emitted as success.
            if asyncio.get_running_loop().time() >= deadline:
                raise ServiceUnavailable(reason="stream_timeout", detail={"timeout_seconds": settings.stream_timeout_seconds})
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
            if status == "STARTED":
                stage_started = time.monotonic()
            record("generation_stage", stage=stage, status=status,
                   elapsed_ms=round((time.monotonic() - started) * 1000),
                   stage_ms=round((time.monotonic() - stage_started) * 1000))
            event = (
                "complete"
                if stage == "COMPLETE"
                else "stage_completed"
                if status == "COMPLETED"
                else "stage_started"
            )
            # Keep intermediate results inside the pipeline; serialize once at COMPLETE.
            payload = {"stage": stage, "status": status}
            if stage == "COMPLETE":
                payload.update(
                    **identity,
                    data=result.model_dump(mode="json") if result is not None else None,
                )
            yield encode_event(
                event,
                sequence,
                payload,
            )
    except asyncio.CancelledError:
        raise  # Client disconnected; cancellation closes in-flight HTTP requests.
    except Exception as exc:
        failure("pipeline_failed", exc, stage=stage,
                elapsed_ms=round((time.monotonic() - started) * 1000))
        if isinstance(exc, ApiError):
            code, message = exc.code, exc.message
        else:
            code, message = (
                "internal_server_error",
                "서버 내부 오류가 발생했습니다.",
            )
        # HTTP headers have already been sent: failure is a terminal SSE event.
        yield encode_event(
            "error",
            sequence + 1,
            {
                **identity,
                "stage": stage,
                "status": "FAILED",
                "message": code,
                "data": {"error_message": message},
            },
        )
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pending
        try:
            await generator.aclose()
        finally:
            log_request_id.reset(token)
