from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_service.auth import require_api_token
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
)
from ai_service.streaming import stream_generation


logger = logging.getLogger(__name__)
ERROR_RESPONSES = {code: {"model": ErrorResponse} for code in (400, 401, 422, 500, 503)}


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
            data = {"user_message": exc.message}
        elif exc.status_code == 422:
            data = {
                "generation_job_id": request.state.generation_job_id,
                "error_message": exc.message,
            }
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
            {"user_message": "잠시 후 다시 시도해주세요."}
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
    async def create_itinerary(body: ItineraryRequest, request: Request):
        request.state.generation_job_id = body.generation_job_id
        if not settings.openai_api_key or not settings.kakao_rest_api_key:
            raise ServiceUnavailable()
        app.state.routes.require_configured(body.preference.transport_type)
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
            200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}},
        },
        tags=["V1"],
    )
    async def stream_itinerary(body: ItineraryStreamRequest, request: Request):
        request.state.generation_job_id = body.generation_job_id
        if not settings.openai_api_key or not settings.kakao_rest_api_key:
            raise ServiceUnavailable()
        app.state.routes.require_configured(body.preference.transport_type)
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

    app.include_router(protected)
    return app


app = create_app()
