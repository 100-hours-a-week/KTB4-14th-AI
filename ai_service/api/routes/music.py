"""여행 조건으로 YouTube 후보를 E5 정렬하는 음악 추천 API."""
import asyncio
from typing import Annotated
from fastapi import APIRouter, Body, Request

from ai_service.api.examples import MUSIC_REQUEST_EXAMPLE, MUSIC_RESPONSE_EXAMPLES
from ai_service.api.responses import ERROR_RESPONSES
from ai_service.core.errors import ServiceUnavailable
from ai_service.music.schemas import MusicRequest, MusicResponse, SelectedMusic


def router(settings):
    routes = APIRouter()
    @routes.post(
        "/internal/ai/music/recommend",
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
                selected = await request.app.state.music_recommender.recommend(body)
                return MusicResponse(data=SelectedMusic(
                    **selected.model_dump(), travel_plan_id=body.travel_plan_id,
                ))
        except TimeoutError as exc:
            raise ServiceUnavailable(reason="music_timeout", detail={"timeout_seconds": settings.generation_timeout_seconds}) from exc

    return routes
