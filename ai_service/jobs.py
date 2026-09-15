from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Lock
from typing import Any, Callable, Protocol
from uuid import uuid4

from ai_service.errors import ApiError, IdempotencyConflict, JobNotFound
from ai_service.features import PIPELINE_VERSION, utc_now
from ai_service.schemas import JobStatus, JobType


JobHandler = Callable[[dict[str, Any]], tuple[dict[str, Any], str]]


class JobBackend(Protocol):
    def submit(
        self,
        job_type: JobType,
        payload: dict[str, Any],
        handler: JobHandler,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    def get(self, job_id: str) -> dict[str, Any]: ...


class LocalJobBackend:
    """Development backend. Replace with Modal calls or a durable queue in production."""

    def __init__(self, *, synchronous: bool = False) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._synchronous = synchronous

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def submit(
        self,
        job_type: JobType,
        payload: dict[str, Any],
        handler: JobHandler,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        fingerprint = self._fingerprint(payload)
        with self._lock:
            if idempotency_key and idempotency_key in self._idempotency:
                saved_fingerprint, saved_job_id = self._idempotency[idempotency_key]
                if saved_fingerprint != fingerprint:
                    raise IdempotencyConflict()
                return self._accepted(saved_job_id, job_type)

            prefix = {
                JobType.ITINERARY_GENERATION: "route",
                JobType.TRAVELER_MATCHING: "match",
                JobType.TRAVEL_VIDEO_GENERATION: "video",
            }[job_type]
            job_id = f"job_{prefix}_{uuid4().hex[:12]}"
            self._jobs[job_id] = {
                "job_id": job_id,
                "job_type": job_type.value,
                "status": JobStatus.QUEUED.value,
                "progress": 0,
                "result": None,
                "error": None,
                "model_version": None,
                "pipeline_version": PIPELINE_VERSION,
                "completed_at": None,
            }
            if idempotency_key:
                self._idempotency[idempotency_key] = (fingerprint, job_id)

        if self._synchronous:
            self._run(job_id, payload, handler)
        else:
            self._executor.submit(self._run, job_id, payload, handler)
        return self._accepted(job_id, job_type)

    @staticmethod
    def _accepted(job_id: str, job_type: JobType) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "job_type": job_type.value,
            "status": JobStatus.QUEUED.value,
            "status_url": f"/api/ai/v1/jobs/{job_id}",
        }

    def _run(
        self, job_id: str, payload: dict[str, Any], handler: JobHandler
    ) -> None:
        with self._lock:
            self._jobs[job_id]["status"] = JobStatus.RUNNING.value
            self._jobs[job_id]["progress"] = 10
        try:
            result, model_version = handler(payload)
        except ApiError as exc:
            self._fail(
                job_id,
                exc.code,
                exc.message,
                retryable=exc.retryable,
                details=exc.details,
            )
        except ValueError as exc:
            code = str(exc)
            message = {
                "NO_MATCH_CANDIDATES": "추천할 동행 후보가 없습니다.",
            }.get(code, "작업 결과를 생성하지 못했습니다.")
            self._fail(job_id, code, message, retryable=False)
        except Exception:
            self._fail(
                job_id,
                "MODEL_EXECUTION_FAILED",
                "모델 실행 중 오류가 발생했습니다.",
                retryable=True,
            )
        else:
            with self._lock:
                self._jobs[job_id].update(
                    status=JobStatus.SUCCEEDED.value,
                    progress=100,
                    result=result,
                    error=None,
                    model_version=model_version,
                    completed_at=utc_now(),
                )

    def _fail(
        self,
        job_id: str,
        code: str,
        message: str,
        *,
        retryable: bool,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        with self._lock:
            self._jobs[job_id].update(
                status=JobStatus.FAILED.value,
                result=None,
                error={
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "details": details or [],
                },
                completed_at=utc_now(),
            )

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise JobNotFound()
            return dict(self._jobs[job_id])

