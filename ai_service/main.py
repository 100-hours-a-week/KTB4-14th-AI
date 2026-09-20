from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from uuid import uuid4
from typing import Annotated

import httpx
from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_service.auth import require_api_token
from ai_service.api_examples import ITINERARY_REQUEST_EXAMPLE, MUSIC_REQUEST_EXAMPLE
from ai_service.config import Settings
from ai_service.errors import ApiError, ServiceUnavailable
from ai_service.features import generate_itinerary
from ai_service.model import OpenAIPlanner
from ai_service.places import KakaoPlaces
from ai_service.routing import KakaoRoutes
from ai_service.schemas import (
    ErrorResponse,
    ItineraryRequest,
    ItineraryResponse,
    ItineraryStreamRequest,
    MusicRequest,
    MusicResponse,
    SelectedMusic,
)
from ai_service.streaming import encode_event, stream_generation
from ai_service.transport import resolve_day_transports


logger = logging.getLogger(__name__)
ERROR_RESPONSES = {code: {"model": ErrorResponse} for code in (400, 401, 422, 500, 503)}
STREAM_RESPONSE = {
    "description": (
        "SSE: PLACES → ACCOMMODATIONS → ROUTES → MUSIC 순서로 처리합니다. "
        "중간 stage_started/stage_completed의 JSON은 stage, status만 포함합니다. "
        "중간 이벤트에는 generation_job_id와 data가 없습니다. "
        "마지막 event: complete(stage: COMPLETE)에서만 generation_job_id, stage, status, "
        "data: {itinerary, music}을 한 번 반환합니다. "
        "실패하면 기존 event: error에 generation_job_id, stage, status: FAILED, message, "
        "data: {error_message}를 반환하고 종료합니다. HTTP 200도 성공을 보장하지 않습니다. "
        "SSE id 순번과 keep-alive 주석은 유지됩니다."
    ),
    "content": {
        "text/event-stream": {
            "schema": {"type": "string"},
            "examples": {
                "stage_started": {
                    "summary": "단계 시작 — 상태만 반환",
                    "value": encode_event("stage_started", 1, {"stage": "PLACES", "status": "STARTED"}),
                },
                "stage_completed": {
                    "summary": "단계 완료 — 결과 본문 없음(네 단계 공통)",
                    "value": encode_event("stage_completed", 2, {"stage": "PLACES", "status": "COMPLETED"}),
                },
                "complete": {
                    "summary": "전체 완료 — 일정·음악 반환(구조 예시, days는 빈 목록)",
                    "value": encode_event("complete", 9, {
                        "generation_job_id": 1, "stage": "COMPLETE", "status": "COMPLETED",
                        "data": {
                            "itinerary": {
                                "generation_job_id": 1, "client_draft_id": 1,
                                "region": {"region_id": 2, "full_name": "부산광역시"},
                                "duration": {"arrival_datetime": "2026-09-19T10:00:00", "departure_datetime": "2026-09-20T18:00:00"},
                                "headcount": 2, "companion_type": "COUPLE",
                                "preference": {"pace_type": "RELAXED", "transport_type": "PUBLIC_TRANSPORT",
                                               "budget_currency": "KRW", "themes": ["NATURE", "FOOD"], "foods": [],
                                               "budget_min": None, "budget_max": None, "distance_preference": None, "extra_request": None},
                                "required_places": [], "title": "부산 여유로운 여행 일정", "days": [],
                            },
                            "music": {"title": "Spring Day", "artist": "BTS",
                                      "youtube_url": "https://www.youtube.com/results?search_query=BTS+Spring+Day+official+audio"},
                        },
                    }),
                },
                "error": {
                    "summary": "기존 오류 형식 유지 — COMPLETE 없이 종료",
                    "value": encode_event("error", 8, {
                        "generation_job_id": 1, "stage": "MUSIC", "status": "FAILED",
                        "message": "ai_music_recommendation_failed",
                        "data": {"error_message": "추천 가능한 음악을 선택하지 못했습니다."},
                    }),
                },
            },
        }
    },
}


def create_app(
    *,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(transport=transport) as client:
            app.state.places = KakaoPlaces(client, settings)
            app.state.planner = OpenAIPlanner(client, settings)
            app.state.routes = KakaoRoutes(client, settings)
            yield

    app = FastAPI(title="Audigo AI API", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request.state.request_id = f"req_{uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        data = None
        if exc.status_code == 503:
            data = {"error_message": exc.message}
        elif exc.status_code == 422:
            data = {"error_message": exc.message}
            if hasattr(request.state, "generation_job_id"):
                data["generation_job_id"] = request.state.generation_job_id
            elif hasattr(request.state, "travel_plan_id"):
                data["travel_plan_id"] = request.state.travel_plan_id
        return JSONResponse(
            status_code=exc.status_code, content={"message": exc.code, "data": data}
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=400, content={"message": "invalid_request", "data": None}
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        code = {
            401: "unauthorized",
            404: "not_found",
            503: "ai_service_unavailable",
        }.get(exc.status_code, "http_error")
        data = (
            {"error_message": "잠시 후 다시 시도해주세요."}
            if exc.status_code == 503
            else None
        )
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={"message": code, "data": data},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        # Log only type and correlation ID; upstream exceptions may contain secrets.
        logger.error(
            "Unexpected %s request_id=%s",
            type(exc).__name__,
            getattr(request.state, "request_id", "unknown"),
        )
        return JSONResponse(
            status_code=500,
            content={"message": "internal_server_error", "data": None},
            headers={"X-Request-Id": getattr(request.state, "request_id", "unknown")},
        )

    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "mode": "live"}

    protected = APIRouter(
        prefix="/internal/ai",
        dependencies=[Depends(require_api_token(settings.api_token))],
    )

    @protected.post(
        "/itineraries/generate",
        response_model=ItineraryResponse,
        responses=ERROR_RESPONSES,
        tags=["V1"],
    )
    async def create_itinerary(
        body: Annotated[ItineraryRequest, Body(openapi_examples={"spreadsheet": {"summary": "스프레드시트 여행 요청", "value": ITINERARY_REQUEST_EXAMPLE}})],
        request: Request,
    ):
        request.state.generation_job_id = body.generation_job_id
        if not settings.openai_api_key or not settings.kakao_rest_api_key:
            raise ServiceUnavailable()
        for mode in set(resolve_day_transports(body).values()):
            app.state.routes.require_configured(mode)
        try:
            async with asyncio.timeout(settings.generation_timeout_seconds):
                return await generate_itinerary(
                    body, app.state.places, app.state.planner, app.state.routes
                )
        except TimeoutError as exc:
            raise ServiceUnavailable() from exc

    @protected.post(
        "/itineraries/generate/stream",
        response_class=StreamingResponse,
        responses={
            **ERROR_RESPONSES,
            200: STREAM_RESPONSE,
        },
        tags=["V1"],
    )
    async def stream_itinerary(
        body: Annotated[ItineraryStreamRequest, Body(openapi_examples={"spreadsheet": {"summary": "스프레드시트 여행 요청으로 단계별 생성", "value": ITINERARY_REQUEST_EXAMPLE}})],
        request: Request,
    ):
        request.state.generation_job_id = body.generation_job_id
        if not settings.openai_api_key or not settings.kakao_rest_api_key:
            raise ServiceUnavailable()
        for mode in set(resolve_day_transports(body).values()):
            app.state.routes.require_configured(mode)
        return StreamingResponse(
            stream_generation(
                body,
                app.state.places,
                app.state.planner,
                settings,
                request.state.request_id,
                app.state.routes,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @protected.post(
        "/music/recommend",
        response_model=MusicResponse,
        responses=ERROR_RESPONSES,
        tags=["V1"],
        description="스프레드시트 음악 요청 전용. candidates 중 한 곡을 선택하고 후보의 ID·제목·가수·URL을 그대로 반환합니다.",
    )
    async def recommend_music(
        body: Annotated[MusicRequest, Body(openapi_examples={"spreadsheet": {"summary": "스프레드시트 음악 후보 요청(URL은 자리표시자)", "value": MUSIC_REQUEST_EXAMPLE}})],
        request: Request,
    ):
        request.state.travel_plan_id = body.travel_plan_id
        try:
            async with asyncio.timeout(settings.generation_timeout_seconds):
                selected = await app.state.planner.select_music(body)
                return MusicResponse(data=SelectedMusic(
                    **selected.model_dump(), travel_plan_id=body.travel_plan_id,
                ))
        except TimeoutError as exc:
            raise ServiceUnavailable() from exc

    app.include_router(protected)
    return app


app = create_app()
