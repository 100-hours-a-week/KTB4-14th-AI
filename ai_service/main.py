from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
from uuid import uuid4
from typing import Annotated

import httpx
from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_service.auth import require_api_token
from ai_service.api_examples import GENERATION_REQUEST_EXAMPLES, MUSIC_REQUEST_EXAMPLE, MUSIC_RESPONSE_EXAMPLES, ROUTE_RESPONSE_EXAMPLE, ITINERARY_RESPONSE_EXAMPLES
from ai_service.config import Settings
from ai_service.diagnostics import request_id as log_request_id, record, failure
from ai_service.errors import ApiError, ServiceUnavailable
from ai_service.e5_music import E5MusicRecommender
from ai_service.pipeline import generate_plan
from ai_service.model import OpenAIPlanner
from ai_service.matching import E5MatchingRecommender, MatchingRequestCreate, MatchingRecommendationResponse
from ai_service.matching_examples import MATCHING_REQUEST_EXAMPLES, MATCHING_RESPONSE_EXAMPLES
from ai_service.matching_source import MockMatchingCandidateSource
from ai_service.music import YouTubeMusic
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
from ai_service.backend_contract import stream_backend_generation
from ai_service.transport import resolve_day_transports
from ai_service.swagger_ui import korean_swagger_html


ERROR_RESPONSES = {
    code: {"model": ErrorResponse, "description": description}
    for code, description in {
        400: "필수 값 누락 또는 잘못된 요청 형식",
        401: "인증 토큰이 없거나 유효하지 않음",
        422: "요청 조건 처리 실패",
        500: "서버 내부 오류",
        503: "모델·서비스 장애 또는 처리 시간 초과",
    }.items()
}
ITINERARY_RESPONSES = {
    code: {
        **ERROR_RESPONSES.get(code, {}),
        "description": example["description"],
        "content": {"application/json": {"examples": {
            "default": {"summary": example["value"].get("message", "여행 일정 생성 성공"), "value": example["value"]},
            **example.get("alternatives", {}),
        }}},
    }
    for code, example in ITINERARY_RESPONSE_EXAMPLES.items()
}
def create_app(
    *,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """외부 API 클라이언트와 인증·오류 처리·엔드포인트를 묶어 앱을 만든다."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(transport=transport) as client:
            app.state.places = KakaoPlaces(client, settings)
            app.state.planner = OpenAIPlanner(client, settings)
            app.state.routes = KakaoRoutes(client, settings)
            app.state.music_recommender = E5MusicRecommender(
                YouTubeMusic(client), settings.e5_model_dir
            )
            app.state.matching_recommender = E5MatchingRecommender(settings.e5_model_dir)
            app.state.matching_candidate_source = MockMatchingCandidateSource()
            yield

    app = FastAPI(title="Audigo AI API", version="1.0.0", lifespan=lifespan, docs_url=None)

    @app.get("/docs", include_in_schema=False)
    async def swagger_docs():
        return korean_swagger_html(openapi_url=app.openapi_url, title="Audigo AI API — API 문서")

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        # 요청별 ID를 응답과 로그에 함께 남겨 장애 추적에 사용한다.
        request.state.request_id = f"req_{uuid4().hex}"
        token = log_request_id.set(request.state.request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-Id"] = request.state.request_id
            return response
        finally:
            log_request_id.reset(token)

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        failure("request_failed", exc, http_status=exc.status_code)
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
        # 두 요청 형식 중 실제 입력에 해당하는 형식의 오류만 사용자에게 보여준다.
        errors = exc.errors()
        record("request_invalid", http_status=400, errors=[{
            "field": ".".join(map(str, error["loc"])), "type": error["type"]
        } for error in errors[:10]])
        branches = {"LegacyGenerationRequest", "TravelGenerationRequest"}
        if isinstance(exc.body, dict):
            branch = ("LegacyGenerationRequest" if "generation_job_id" in exc.body or "region" in exc.body
                      else "TravelGenerationRequest")
            selected = [error for error in errors if any(branch in str(part) for part in error["loc"])]
            errors = selected or errors
        descriptions = []
        for error in errors[:5]:
            # 입력값·본문·예외 내부 정보는 오류 응답에 노출하지 않는다.
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
        # 상위 서비스 예외에는 비밀값이 있을 수 있어 유형과 요청 ID만 기록한다.
        failure("unexpected_error", exc, request_id=getattr(request.state, "request_id", "unknown"))
        return JSONResponse(
            status_code=500,
            content={"message": "internal_server_error", "data": None},
            headers={"X-Request-Id": getattr(request.state, "request_id", "unknown")},
        )

    @app.get("/health", tags=["system"], summary="서버 상태 확인", response_description="서버 정상")
    async def health():
        return {"status": "ok", "mode": "live"}

    protected = APIRouter(
        prefix="/internal/ai",
        dependencies=[Depends(require_api_token(settings.api_token))],
    )

    @protected.post(
        "/itineraries/generate",
        response_model=ItineraryResponse,
        responses=ITINERARY_RESPONSES,
        tags=["V1"],
        summary="여행 일정 생성",
    )
    async def create_itinerary(
        body: Annotated[LegacyGenerationRequest | TravelGenerationRequest, Body(openapi_examples=GENERATION_REQUEST_EXAMPLES)],
        request: Request,
    ):
        # 두 입력 DTO를 공통 생성 요청으로 변환한 뒤 제한 시간 안에 일정을 만든다.
        if isinstance(body, LegacyGenerationRequest):
            request.state.generation_job_id = body.generation_job_id
        else:
            request.state.travel_plan_id = body.travel_plan_id
        body = body.generation_context()
        if not settings.openai_api_key or not settings.kakao_rest_api_key:
            raise ServiceUnavailable(reason="provider_keys_missing")
        for mode in set(resolve_day_transports(body).values()):
            app.state.routes.require_configured(mode)
        try:
            async with asyncio.timeout(settings.generation_timeout_seconds):
                result = await generate_plan(
                    body, app.state.places, app.state.planner, app.state.routes
                )
                return result.itinerary
        except TimeoutError as exc:
            raise ServiceUnavailable(reason="generation_timeout", detail={"timeout_seconds": settings.generation_timeout_seconds}) from exc

    backend_router = APIRouter(dependencies=[Depends(require_api_token(settings.api_token))])

    @backend_router.post(
        "/api/ai/v1/itinerary-jobs/stream",
        response_class=StreamingResponse,
        responses={
            **{code: response for code, response in ITINERARY_RESPONSES.items() if code != 200},
            200: {"description": "단계별 SSE. 최종 결과는 ROUTE_OPTIMIZE_DONE.result에 한 번 전송합니다. HTTP 200 이후 실패는 error 이벤트로 전달합니다.",
                  "content": {"text/event-stream": {"schema": {"type": "string"}}}},
        },
        tags=["V1"],
        summary="여행 일정 생성 (실시간 SSE)",
    )
    async def stream_itinerary(
        body: Annotated[LegacyGenerationRequest | TravelGenerationRequest, Body(openapi_examples=GENERATION_REQUEST_EXAMPLES)],
        request: Request,
    ):
        # 백엔드가 소비하는 SSE 형식으로 단계별 생성 상태를 보낸다.
        if isinstance(body, LegacyGenerationRequest):
            request.state.generation_job_id = body.generation_job_id
        else:
            request.state.travel_plan_id = body.travel_plan_id
        body = body.generation_context()
        if not settings.openai_api_key or not settings.kakao_rest_api_key:
            raise ServiceUnavailable(reason="provider_keys_missing")
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
        summary="여행 음악 추천 (E5)",
        description="여행 시작일의 한국 시간 월에 맞는 계절을 반영해 YouTube 실시간 후보를 multilingual-e5-small로 정렬하고, 검증된 음악 영상 한 개를 반환합니다. 후보 목록은 받지 않습니다.",
    )
    async def recommend_music(
        body: Annotated[MusicRequest, Body(openapi_examples={"travel": {"summary": "여행 정보 기반 음악 추천", "value": MUSIC_REQUEST_EXAMPLE}})],
        request: Request,
    ):
        request.state.travel_plan_id = body.travel_plan_id
        try:
            async with asyncio.timeout(settings.generation_timeout_seconds):
                selected = await app.state.music_recommender.recommend(body)
                return MusicResponse(data=SelectedMusic(
                    **selected.model_dump(), travel_plan_id=body.travel_plan_id,
                ))
        except TimeoutError as exc:
            raise ServiceUnavailable(reason="music_timeout", detail={"timeout_seconds": settings.generation_timeout_seconds}) from exc

    @backend_router.post(
        "/matching-requests",
        response_model=MatchingRecommendationResponse,
        responses={
            code: {
                **ERROR_RESPONSES.get(code, {}),
                "content": {"application/json": {"examples": examples}},
            }
            for code, examples in MATCHING_RESPONSE_EXAMPLES.items()
        },
        tags=["V2"],
        summary="동행자 후보 추천 (E5)",
        response_description="동행자 후보 추천 성공",
        operation_id="create_matching_request_v2",
    )
    async def recommend_companions(
        body: Annotated[MatchingRequestCreate, Body(openapi_examples=MATCHING_REQUEST_EXAMPLES)],
        request: Request,
    ):
        try:
            async with asyncio.timeout(settings.model_timeout_seconds):
                recommendation = await request.app.state.matching_candidate_source.recommendation_request(body)
                return await request.app.state.matching_recommender.recommend(recommendation)
        except TimeoutError as exc:
            raise ServiceUnavailable(reason="matching_timeout") from exc

    app.include_router(protected)
    app.include_router(backend_router)

    default_openapi = app.openapi

    def openapi_with_response_examples():
        schema = default_openapi()
        responses = schema["paths"]["/internal/ai/music/recommend"]["post"]["responses"]
        # FastAPI가 예시의 data:null을 생략하므로 직렬화 후 원래 응답 예시를 복원한다.
        for code, example in MUSIC_RESPONSE_EXAMPLES.items():
            responses[str(code)]["content"]["application/json"]["example"] = deepcopy(example["value"])
        responses = schema["paths"]["/internal/ai/itineraries/generate"]["post"]["responses"]
        for code, response in ITINERARY_RESPONSES.items():
            responses[str(code)]["content"]["application/json"] = deepcopy(response["content"]["application/json"])
        schema["components"]["schemas"]["RouteSummary"]["examples"] = [deepcopy(ROUTE_RESPONSE_EXAMPLE)]
        # 실제 요청 검증 오류는 공통 핸들러가 400으로 반환한다.
        matching_operation = schema["paths"]["/matching-requests"]["post"]
        matching_operation["responses"].pop("422", None)
        matching_operation["requestBody"]["content"]["application/json"]["examples"] = deepcopy(MATCHING_REQUEST_EXAMPLES)
        for code, examples in MATCHING_RESPONSE_EXAMPLES.items():
            matching_operation["responses"][str(code)]["content"]["application/json"]["examples"] = deepcopy(examples)
        tags = schema.setdefault("tags", [])
        if not any(tag["name"] == "V2" for tag in tags):
            tags.append({"name": "V2"})
        return schema

    app.openapi = openapi_with_response_examples
    return app


app = create_app()
