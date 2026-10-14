"""FastAPI 앱 조립. 실행 진입점은 ai_service.main:app."""
from fastapi import Depends, FastAPI
import httpx

from ai_service.api.routes import jobs, matching, music, system, travel
from ai_service.api.openapi import configure_openapi
from ai_service.core.auth import require_api_token
from ai_service.core.config import Settings
from ai_service.core.handlers import register_exception_handlers
from ai_service.core.lifespan import create_lifespan
from ai_service.core.middleware import register_request_id


def create_app(*, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Audigo AI API", version="1.0.0", docs_url=None,
                  lifespan=create_lifespan(settings, transport))
    app.state.settings = settings
    register_request_id(app)
    register_exception_handlers(app)
    app.include_router(system.router)
    auth = [Depends(require_api_token(settings.api_token))]
    for factory in (travel.router, music.router, matching.router, jobs.router):
        app.include_router(factory(settings), dependencies=auth)
    configure_openapi(app)
    return app


app = create_app()
