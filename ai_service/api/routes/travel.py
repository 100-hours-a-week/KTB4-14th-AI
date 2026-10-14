"""일정 생성 JSON·SSE API. 두 경로는 동일한 4단계 서비스를 사용한다."""
import asyncio
from typing import Annotated
from fastapi import APIRouter, Body, Request
from fastapi.responses import StreamingResponse

from ai_service.api.deps import prepare_generation
from ai_service.api.examples import GENERATION_REQUEST_EXAMPLES
from ai_service.api.responses import ITINERARY_RESPONSES
from ai_service.core.errors import ServiceUnavailable
from ai_service.travel.schemas import ItineraryResponse, LegacyGenerationRequest, TravelGenerationRequest
from ai_service.travel.pipeline import generate_plan
from ai_service.travel.backend_contract import stream_backend_generation


def router(settings):
    routes = APIRouter()
    @routes.post(
        "/internal/ai/itineraries/generate",
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
        body = prepare_generation(body, request, settings)
        try:
            async with asyncio.timeout(settings.generation_timeout_seconds):
                result = await generate_plan(
                    body, request.app.state.places, request.app.state.planner, request.app.state.routes
                )
                return result.itinerary
        except TimeoutError as exc:
            raise ServiceUnavailable(reason="generation_timeout", detail={"timeout_seconds": settings.generation_timeout_seconds}) from exc

    @routes.post(
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
        body = prepare_generation(body, request, settings)
        return StreamingResponse(
            stream_backend_generation(
                body,
                request.app.state.places,
                request.app.state.planner,
                settings,
                request.state.request_id,
                request.app.state.routes,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return routes
