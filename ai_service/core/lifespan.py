"""앱 시작·종료 시 외부 클라이언트와 추천 서비스의 수명을 관리한다."""
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI

from ai_service.core.config import Settings
from ai_service.services.music import E5MusicRecommender
from ai_service.clients.youtube import YouTubeMusic
from ai_service.planners.travel import OpenAIPlanner
from ai_service.clients.kakao_places import KakaoPlaces
from ai_service.clients.kakao_routes import KakaoRoutes


def create_lifespan(settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(transport=transport) as client:
            app.state.places = KakaoPlaces(client, settings)
            app.state.planner = OpenAIPlanner(client, settings)
            app.state.routes = KakaoRoutes(client, settings)
            app.state.music_recommender = E5MusicRecommender(
                YouTubeMusic(client), settings.e5_model_dir
            )
            yield

    return lifespan
