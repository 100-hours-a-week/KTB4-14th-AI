from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

import modal


image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_pip_install(
        "fastapi==0.117.1",
        "httpx==0.28.1",
        "pydantic==2.11.9",
        "uvicorn[standard]==0.36.0",
    )
    .add_local_python_source("ai_service")
)
app = modal.App("audigo-ai-api")
secrets = modal.Secret.from_name(
    "audigo-ai-secrets", required_keys=["AUDIGO_API_TOKEN", "AUDIGO_MODE"]
)
job_store = modal.Dict.from_name("audigo-ai-jobs", create_if_missing=True)

# V1 uses a small music catalog and an external Gemini API, so no GPU is needed.
# Tuples specify (requested resources, resource limit), per container.
V1_RESOURCES = {"cpu": (0.25, 1.0), "memory": (256, 512)}


def _error(exc: Exception) -> dict[str, Any]:
    from ai_service.errors import ApiError

    if isinstance(exc, ApiError):
        return {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "details": exc.details,
        }
    if isinstance(exc, ValueError) and str(exc) == "NO_MATCH_CANDIDATES":
        return {
            "code": "NO_MATCH_CANDIDATES",
            "message": "추천할 동행 후보가 없습니다.",
            "retryable": False,
            "details": [],
        }
    return {
        "code": "MODEL_EXECUTION_FAILED",
        "message": "모델 실행 중 오류가 발생했습니다.",
        "retryable": True,
        "details": [],
    }


@app.function(
    image=image,
    secrets=[secrets],
    **V1_RESOURCES,
    min_containers=0,
    max_containers=2,
    buffer_containers=0,
    scaledown_window=60,
    timeout=120,
    retries=0,
)
def run_job(job_id: str, job_type: str, payload: dict[str, Any]) -> None:
    from ai_service.config import Settings
    from ai_service.features import (
        generate_itinerary,
        generate_video,
        rank_travelers,
        utc_now,
    )

    record = job_store[job_id]
    record.update(status="RUNNING", progress=10)
    job_store[job_id] = record
    try:
        if job_type == "ITINERARY_GENERATION":
            result, model_version = generate_itinerary(payload, Settings.from_env())
        elif job_type == "TRAVELER_MATCHING":
            result, model_version = rank_travelers(payload)
        elif job_type == "TRAVEL_VIDEO_GENERATION":
            result, model_version = generate_video(payload)
        else:
            raise ValueError("UNKNOWN_JOB_TYPE")
    except Exception as exc:
        record.update(
            status="FAILED",
            result=None,
            error=_error(exc),
            completed_at=utc_now(),
        )
    else:
        record.update(
            status="SUCCEEDED",
            progress=100,
            result=result,
            error=None,
            model_version=model_version,
            completed_at=utc_now(),
        )
    job_store[job_id] = record


class ModalJobBackend:
    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()

    def submit(
        self,
        job_type,
        payload: dict[str, Any],
        handler,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        from ai_service.errors import IdempotencyConflict
        from ai_service.features import PIPELINE_VERSION

        fingerprint = self._fingerprint(payload)
        idempotency_storage_key = None
        if idempotency_key:
            key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
            idempotency_storage_key = f"idempotency:{key_hash}"
            saved = job_store.get(idempotency_storage_key)
            if saved:
                if saved["fingerprint"] != fingerprint:
                    raise IdempotencyConflict()
                return self._accepted(saved["job_id"], job_type.value)

        prefix = {
            "ITINERARY_GENERATION": "route",
            "TRAVELER_MATCHING": "match",
            "TRAVEL_VIDEO_GENERATION": "video",
        }[job_type.value]
        job_id = f"job_{prefix}_{uuid4().hex[:12]}"
        job_store[job_id] = {
            "job_id": job_id,
            "job_type": job_type.value,
            "status": "QUEUED",
            "progress": 0,
            "result": None,
            "error": None,
            "model_version": None,
            "pipeline_version": PIPELINE_VERSION,
            "completed_at": None,
        }
        if idempotency_storage_key:
            job_store[idempotency_storage_key] = {
                "fingerprint": fingerprint,
                "job_id": job_id,
            }
        try:
            run_job.spawn(job_id, job_type.value, payload)
        except Exception:
            record = job_store[job_id]
            record.update(
                status="FAILED",
                error={
                    "code": "JOB_SUBMISSION_FAILED",
                    "message": "작업을 실행 환경에 등록하지 못했습니다.",
                    "retryable": True,
                    "details": [],
                },
            )
            job_store[job_id] = record
            raise
        return self._accepted(job_id, job_type.value)

    @staticmethod
    def _accepted(job_id: str, job_type: str) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "job_type": job_type,
            "status": "QUEUED",
            "status_url": f"/api/ai/v1/jobs/{job_id}",
        }

    def get(self, job_id: str) -> dict[str, Any]:
        from ai_service.errors import JobNotFound

        record = job_store.get(job_id)
        if record is None:
            raise JobNotFound()
        return record


@app.function(
    image=image,
    secrets=[secrets],
    **V1_RESOURCES,
    min_containers=0,
    max_containers=1,
    buffer_containers=0,
    scaledown_window=60,
    timeout=60,
)
@modal.concurrent(max_inputs=10)
@modal.asgi_app()
def web():
    from ai_service.config import Settings
    from ai_service.main import create_app

    return create_app(settings=Settings.from_env(), jobs=ModalJobBackend())
