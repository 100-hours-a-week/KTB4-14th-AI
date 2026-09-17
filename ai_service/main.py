from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_service.auth import require_api_token
from ai_service.config import Settings
from ai_service.errors import ApiError
from ai_service.features import (
    generate_checklist,
    generate_trip_pipeline,
    generate_video,
    rank_travelers,
    recommend_music,
)
from ai_service.jobs import JobBackend, LocalJobBackend
from ai_service.schemas import (
    ChecklistRequest,
    ChecklistResponse,
    ErrorResponse,
    ItineraryRequest,
    JobAccepted,
    JobResponse,
    JobStatus,
    JobType,
    MatchRequest,
    MusicRequest,
    MusicResponse,
    VideoRequest,
)


ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


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


def _error_body(
    request: Request,
    code: str,
    message: str,
    retryable: bool = False,
    details: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": _normalize_details(details),
        },
        "request_id": getattr(request.state, "request_id", f"req_{uuid4().hex}"),
    }


def create_app(
    *,
    settings: Settings | None = None,
    jobs: JobBackend | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    jobs = jobs or LocalJobBackend()
    app = FastAPI(
        title="Audigo AI API",
        version="0.2.0",
        description=(
            "Audigo AI 개발 API. 여행 일정 job은 SSE로 장소·식당 추천, 숙소 위치, "
            "이동 경로, 음악 추천 단계 결과를 순서대로 전달합니다."
        ),
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request.state.request_id = f"req_{uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(
                request, exc.code, exc.message, exc.retryable, exc.details
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        details = []
        for item in exc.errors():
            location = [str(part) for part in item["loc"] if part != "body"]
            details.append(
                {"field": ".".join(location), "message": item["msg"]}
            )
        return JSONResponse(
            status_code=400,
            content=_error_body(
                request,
                "INVALID_REQUEST",
                "입력값을 확인해 주세요.",
                details=details,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        code = {
            401: "UNAUTHORIZED",
            404: "NOT_FOUND",
            503: "SERVICE_NOT_CONFIGURED",
        }.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content=_error_body(request, code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content=_error_body(
                request,
                "INTERNAL_SERVER_ERROR",
                "서버 내부 오류가 발생했습니다.",
                retryable=True,
            ),
        )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": settings.mode}

    protected = APIRouter(
        prefix="/api/ai/v1",
        dependencies=[Depends(require_api_token(settings.api_token))],
    )

    @protected.post(
        "/itinerary-jobs",
        response_model=JobAccepted,
        status_code=202,
        responses=ERROR_RESPONSES,
        tags=["V1"],
    )
    async def create_itinerary_job(
        body: ItineraryRequest,
        idempotency_key: str = Header(
            min_length=8, max_length=128, alias="Idempotency-Key"
        ),
    ):
        payload = body.model_dump(mode="json")
        return jobs.submit(
            JobType.ITINERARY_GENERATION,
            payload,
            lambda job_payload, emit: generate_trip_pipeline(
                job_payload, settings, emit
            ),
            idempotency_key=idempotency_key,
        )

    @protected.get(
        "/jobs/{job_id}",
        response_model=JobResponse,
        responses=ERROR_RESPONSES,
        tags=["common"],
    )
    async def get_job(job_id: str):
        return jobs.get(job_id)

    @protected.get(
        "/jobs/{job_id}/events",
        responses=ERROR_RESPONSES,
        tags=["common"],
    )
    def stream_job_events(
        job_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        jobs.get(job_id)
        try:
            start_after_id = max(0, int(last_event_id or "0"))
        except ValueError:
            start_after_id = 0

        def event_stream():
            cursor = start_after_id
            last_heartbeat = time.monotonic()
            while True:
                events = jobs.events_since(job_id, cursor)
                for event in events:
                    cursor = event["id"]
                    payload = json.dumps(
                        event["data"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    )
                    yield (
                        f"id: {event['id']}\n"
                        f"event: {event['event']}\n"
                        f"data: {payload}\n\n"
                    )
                    last_heartbeat = time.monotonic()

                snapshot = jobs.get(job_id)
                if snapshot["status"] in {
                    JobStatus.SUCCEEDED.value,
                    JobStatus.FAILED.value,
                }:
                    if not jobs.events_since(job_id, cursor):
                        break

                now = time.monotonic()
                if now - last_heartbeat >= 15:
                    yield ": keep-alive\n\n"
                    last_heartbeat = now
                time.sleep(0.25)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @protected.post(
        "/music-recommendations",
        response_model=MusicResponse,
        responses=ERROR_RESPONSES,
        tags=["V1"],
    )
    async def create_music_recommendation(body: MusicRequest):
        try:
            return recommend_music(body.model_dump(mode="json"))
        except ValueError as exc:
            if str(exc) == "NO_MUSIC_CANDIDATES":
                raise ApiError(
                    422,
                    "NO_MUSIC_CANDIDATES",
                    "추천 가능한 음악이 없습니다.",
                ) from exc
            raise

    @protected.post(
        "/traveler-match-jobs",
        response_model=JobAccepted,
        status_code=202,
        responses=ERROR_RESPONSES,
        tags=["V2"],
    )
    async def create_match_job(body: MatchRequest):
        return jobs.submit(
            JobType.TRAVELER_MATCHING,
            body.model_dump(mode="json"),
            lambda payload, emit: rank_travelers(payload),
        )

    @protected.post(
        "/checklists",
        response_model=ChecklistResponse,
        responses=ERROR_RESPONSES,
        tags=["V3"],
    )
    async def create_checklist(body: ChecklistRequest):
        return generate_checklist(body.model_dump(mode="json"), settings)

    @protected.post(
        "/video-jobs",
        response_model=JobAccepted,
        status_code=202,
        responses=ERROR_RESPONSES,
        tags=["V3"],
    )
    async def create_video_job(body: VideoRequest):
        return jobs.submit(
            JobType.TRAVEL_VIDEO_GENERATION,
            body.model_dump(mode="json"),
            lambda payload, emit: generate_video(payload),
        )

    app.include_router(protected)
    return app


app = create_app()
