"""앱 시작·종료 시 외부 클라이언트와 추천 서비스의 수명을 관리한다."""
import asyncio
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI

from ai_service.core.config import Settings
from ai_service.music.e5 import E5MusicRecommender
from ai_service.music.youtube import YouTubeMusic
from ai_service.travel.model import OpenAIPlanner
from ai_service.travel.places import KakaoPlaces
from ai_service.travel.routing import KakaoRoutes
from ai_service.jobs.service import make_job_service


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
            app.state.jobs = make_job_service(settings)
            try:
                yield
            finally:
                if app.state.jobs is not None:
                    await asyncio.to_thread(app.state.jobs.store.close)

    return lifespan
