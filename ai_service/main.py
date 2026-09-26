from __future__ import annotations

import asyncio
from copy import deepcopy
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
from ai_service.api_examples import GENERATION_REQUEST_EXAMPLES, MUSIC_REQUEST_EXAMPLE, MUSIC_RESPONSE_EXAMPLES, ROUTE_RESPONSE_EXAMPLE
from ai_service.config import Settings
from ai_service.errors import ApiError, ServiceUnavailable
from ai_service.features import generate_itinerary
from ai_service.model import OpenAIPlanner
from ai_service.places import KakaoPlaces
from ai_service.routing import KakaoRoutes
from ai_service.schemas import (
    ErrorResponse,
    ItineraryResponse,
    MusicRequest,
    MusicResponse,
    SelectedMusic,
    TravelGenerationRequest,
    LegacyGenerationRequest,
)
from ai_service.streaming import encode_event
from ai_service.backend_contract import stream_backend_generation
from ai_service.transport import resolve_day_transports


logger = logging.getLogger(__name__)
ERROR_RESPONSES = {code: {"model": ErrorResponse} for code in (400, 401, 422, 500, 503)}
STREAM_RESPONSE = {
    "description": (
        "feature-travel SSE: PLACE_RECOMMEND, STAY_RECOMMEND, ROUTE_OPTIMIZE, MUSIC_RECOMMEND. "
        "단계별 STARTED/DONE 이벤트를 전송합니다. 최종 일정은 음악 생성 성공 후 "
        "ROUTE_OPTIMIZE_DONE의 result에 한 번만 전송하며, complete는 완료 상태만 보냅니다. "
        "백엔드가 ROUTE_OPTIMIZE_DONE 수신 즉시 저장하므로 이 이벤트는 음악 성공까지 지연합니다. "
        "result.days[].routes[].legs는 sequence 순서로 도보·버스·지하철 구간을 제공합니다. boarding_stop/alighting_stop에는 이름·검증된 station_number(미확인 null)·버스 번호 배열이 포함됩니다. vehicle_number는 버스 번호 배열, line_name은 지하철 노선 배열입니다. 각 구간에 duration_minute와 distance_meter를 제공합니다. route.total_fare_amount는 선택한 카카오 경로 전체 요금이며 구간 합산하지 않습니다. "
        "HTTP 200 이후에도 error 이벤트로 실패할 수 있습니다. 인증 Bearer 토큰이 필요합니다."
    ),
    "content": {"text/event-stream": {"schema": {"type": "string"}, "examples": {
        "started": {"value": encode_event("PLACE_RECOMMEND_STARTED", 1, {"stage": "PLACE_RECOMMEND", "status": "RUNNING"})},
        "done": {"value": encode_event("PLACE_RECOMMEND_DONE", 2, {"stage": "PLACE_RECOMMEND", "status": "DONE"})},
        "result": {"summary": "승하차·요금 구조 예시. 방문 항목은 생략했으며 이름·요금은 실제 조회 결과가 아닙니다.", "value": encode_event("ROUTE_OPTIMIZE_DONE", 8, {
            "travel_plan_id": 10, "stage": "ROUTE_OPTIMIZE", "status": "DONE",
            "result": {"title": "여행 일정", "days": [{"day_number": 1, "travel_date": "2026-09-25", "items": [],
                "routes": [{"from_sequence": 1, "to_sequence": 2, "order": 1, **ROUTE_RESPONSE_EXAMPLE}]}], "music": {
                "title": "Spring Day", "artist": "BTS", "youtube_url": "https://www.youtube.com/watch?v=xEeFrLSkMm8"
            }},
        })},
        "complete": {"value": encode_event("complete", 9, {"travel_plan_id": 10, "stage": "COMPLETE", "status": "COMPLETED"})},
        "error": {"value": encode_event("error", 8, {
            "travel_plan_id": 10, "stage": "MUSIC_RECOMMEND", "status": "FAILED",
            "message": "ai_music_recommendation_failed", "data": {"error_message": "추천 가능한 음악을 선택하지 못했습니다."},
        })},
    }}},
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
        logger.warning(
            "API error: code=%s status=%s message=%s request_id=%s",
            exc.code,
            exc.status_code,
            exc.message,
            getattr(request.state, "request_id", None),
        )
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
        errors = exc.errors()
        branches = {"LegacyGenerationRequest", "TravelGenerationRequest"}
        if isinstance(exc.body, dict):
            branch = ("LegacyGenerationRequest" if "generation_job_id" in exc.body or "region" in exc.body
                      else "TravelGenerationRequest")
            selected = [error for error in errors if any(branch in str(part) for part in error["loc"])]
            errors = selected or errors
        descriptions = []
        for error in errors[:5]:
            # Never echo input values, the request body, or exception contexts.
            path = ".".join(str(part) for part in error["loc"] if part != "body" and not any(name in str(part) for name in branches)) or "body"
            reason = {
                "missing": "필수 값이 없습니다.",
                "extra_forbidden": "이 요청 형식에서 사용하지 않는 필드입니다.",
                "json_invalid": "올바른 JSON 문법이 아닙니다. 역슬래시·따옴표·쉼표를 확인해주세요.",
            }.get(error["type"], "값의 자료형·형식·허용 범위를 확인해주세요.")
            descriptions.append(f"{path}: {reason}")
        return JSONResponse(
            status_code=400, content={"message": "invalid_request", "data": {"error_message": " / ".join(descriptions)}}
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
        body: Annotated[LegacyGenerationRequest | TravelGenerationRequest, Body(openapi_examples=GENERATION_REQUEST_EXAMPLES)],
        request: Request,
    ):
        if isinstance(body, LegacyGenerationRequest):
            request.state.generation_job_id = body.generation_job_id
        else:
            request.state.travel_plan_id = body.travel_plan_id
        body = body.generation_context()
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

    backend_router = APIRouter(dependencies=[Depends(require_api_token(settings.api_token))])

    @backend_router.post(
        "/api/ai/v1/itinerary-jobs/stream",
        response_class=StreamingResponse,
        responses={
            **ERROR_RESPONSES,
            200: STREAM_RESPONSE,
        },
        tags=["V1"],
    )
    async def stream_itinerary(
        body: Annotated[LegacyGenerationRequest | TravelGenerationRequest, Body(openapi_examples=GENERATION_REQUEST_EXAMPLES)],
        request: Request,
    ):
        if isinstance(body, LegacyGenerationRequest):
            request.state.generation_job_id = body.generation_job_id
        else:
            request.state.travel_plan_id = body.travel_plan_id
        body = body.generation_context()
        if not settings.openai_api_key or not settings.kakao_rest_api_key:
            raise ServiceUnavailable()
        for mode in set(resolve_day_transports(body).values()):
            app.state.routes.require_configured(mode)
        return StreamingResponse(
            stream_backend_generation(
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
        responses={
            code: {
                **ERROR_RESPONSES.get(code, {}),
                "description": example["description"],
                "content": {"application/json": {"example": example["value"]}},
            }
            for code, example in MUSIC_RESPONSE_EXAMPLES.items()
        },
        tags=["V1"],
        description="여행 지역·기간·테마에 맞는 곡 한 개를 추천하고 실제 YouTube 영상 링크를 반환합니다. 후보 목록은 받지 않습니다.",
    )
    async def recommend_music(
        body: Annotated[MusicRequest, Body(openapi_examples={"travel": {"summary": "여행 정보 기반 음악 추천", "value": MUSIC_REQUEST_EXAMPLE}})],
        request: Request,
    ):
        request.state.travel_plan_id = body.travel_plan_id
        try:
            async with asyncio.timeout(settings.generation_timeout_seconds):
                selected = await app.state.planner.recommend_music(body)
                return MusicResponse(data=SelectedMusic(
                    **selected.model_dump(), travel_plan_id=body.travel_plan_id,
                ))
        except TimeoutError as exc:
            raise ServiceUnavailable() from exc

    app.include_router(protected)
    app.include_router(backend_router)

    default_openapi = app.openapi

    def openapi_with_response_examples():
        schema = default_openapi()
        responses = schema["paths"]["/internal/ai/music/recommend"]["post"]["responses"]
        # FastAPI's OpenAPI encoder drops None, including explicit data:null in
        # examples. Restore literal response bodies after schema serialization.
        for code, example in MUSIC_RESPONSE_EXAMPLES.items():
            responses[str(code)]["content"]["application/json"]["example"] = deepcopy(example["value"])
        schema["components"]["schemas"]["RouteSummary"]["examples"] = [deepcopy(ROUTE_RESPONSE_EXAMPLE)]
        return schema

    app.openapi = openapi_with_response_examples
    return app


app = create_app()
