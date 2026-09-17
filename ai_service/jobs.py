from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Lock
from typing import Any, Callable, Protocol
from uuid import uuid4

from ai_service.errors import ApiError, IdempotencyConflict, JobNotFound
from ai_service.features import PIPELINE_VERSION, sanitize_public_result, utc_now
from ai_service.schemas import JobStatus, JobType


StageEmitter = Callable[[str, int, dict[str, Any]], None]
JobHandler = Callable[[dict[str, Any], StageEmitter], tuple[dict[str, Any], str]]


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

    def events_since(self, job_id: str, after_id: int = 0) -> list[dict[str, Any]]: ...


class LocalJobBackend:
    """In-memory job storage used for local development and API tests."""

    def __init__(self, *, synchronous: bool = False) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._synchronous = synchronous

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _append_event_locked(
        self,
        job_id: str,
        event: str,
        data: dict[str, Any],
    ) -> None:
        events = self._events[job_id]
        events.append(
            {
                "id": len(events) + 1,
                "event": event,
                "data": sanitize_public_result(data),
            }
        )

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
                "stage": None,
                "result": None,
                "error": None,
                "model_version": None,
                "pipeline_version": PIPELINE_VERSION,
                "completed_at": None,
            }
            self._events[job_id] = []
            self._append_event_locked(
                job_id,
                "queued",
                {
                    "job_id": job_id,
                    "status": JobStatus.QUEUED.value,
                    "progress": 0,
                },
            )
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
            "events_url": f"/api/ai/v1/jobs/{job_id}/events",
        }

    def _emit_stage(
        self,
        job_id: str,
        stage: str,
        progress: int,
        data: dict[str, Any],
    ) -> None:
        public_data = sanitize_public_result(data)
        with self._lock:
            self._jobs[job_id]["status"] = JobStatus.RUNNING.value
            self._jobs[job_id]["progress"] = max(0, min(progress, 99))
            self._jobs[job_id]["stage"] = stage
            self._append_event_locked(
                job_id,
                "stage",
                {
                    "job_id": job_id,
                    "status": JobStatus.RUNNING.value,
                    "stage": stage,
                    "progress": self._jobs[job_id]["progress"],
                    "result": public_data,
                },
            )

    def _run(
        self, job_id: str, payload: dict[str, Any], handler: JobHandler
    ) -> None:
        with self._lock:
            self._jobs[job_id]["status"] = JobStatus.RUNNING.value
            self._jobs[job_id]["progress"] = 10
            self._append_event_locked(
                job_id,
                "running",
                {
                    "job_id": job_id,
                    "status": JobStatus.RUNNING.value,
                    "progress": 10,
                },
            )
        try:
            result, model_version = handler(
                payload,
                lambda stage, progress, data: self._emit_stage(
                    job_id, stage, progress, data
                ),
            )
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
                "NO_MUSIC_CANDIDATES": "추천 가능한 음악이 없습니다.",
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
            public_result = sanitize_public_result(result)
            with self._lock:
                self._jobs[job_id].update(
                    status=JobStatus.SUCCEEDED.value,
                    progress=100,
                    stage="COMPLETED",
                    result=public_result,
                    error=None,
                    model_version=model_version,
                    completed_at=utc_now(),
                )
                self._append_event_locked(
                    job_id,
                    "completed",
                    {
                        "job_id": job_id,
                        "status": JobStatus.SUCCEEDED.value,
                        "stage": "COMPLETED",
                        "progress": 100,
                        "result": public_result,
                    },
                )

    @staticmethod
    def _normalize_details(
        details: list[dict[str, str]] | None,
    ) -> list[dict[str, str]]:
        normalized = []
        for detail in details or []:
            normalized.append(
                {
                    "field": detail.get("field", ""),
                    "message": detail.get("message") or detail.get("reason", ""),
                }
            )
        return normalized

    def _fail(
        self,
        job_id: str,
        code: str,
        message: str,
        *,
        retryable: bool,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        error = {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": self._normalize_details(details),
        }
        with self._lock:
            self._jobs[job_id].update(
                status=JobStatus.FAILED.value,
                result=None,
                error=error,
                completed_at=utc_now(),
            )
            self._append_event_locked(
                job_id,
                "failed",
                {
                    "job_id": job_id,
                    "status": JobStatus.FAILED.value,
                    "progress": self._jobs[job_id]["progress"],
                    "stage": self._jobs[job_id]["stage"],
                    "error": error,
                },
            )

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise JobNotFound()
            return dict(self._jobs[job_id])

    def events_since(self, job_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            if job_id not in self._jobs:
                raise JobNotFound()
            return [
                {
                    "id": event["id"],
                    "event": event["event"],
                    "data": dict(event["data"]),
                }
                for event in self._events[job_id]
                if event["id"] > after_id
            ]
