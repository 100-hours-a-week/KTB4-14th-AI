"""RunPod video-service entrypoint; PostgreSQL and worker run as separate processes."""
from fastapi import Depends, FastAPI, HTTPException
from ai_service.api.routes.video_jobs import playback_router, router
from ai_service.core.auth import require_api_token
from ai_service.core.config import Settings
from ai_service.core.handlers import register_exception_handlers
from ai_service.core.middleware import register_request_id
from ai_service.services.video.jobs import connection


def create_app():
    settings = Settings.from_env()
    app = FastAPI(title='Audigo Video Service', version='1.0.0', docs_url='/docs', redoc_url=None)
    register_request_id(app)
    register_exception_handlers(app)
    app.include_router(router(settings), dependencies=[Depends(require_api_token(settings.api_token))])
    app.include_router(playback_router())

    @app.get('/healthz', include_in_schema=False)
    def health():
        return {'status': 'alive'}

    @app.get('/readyz', include_in_schema=False)
    def ready():
        try:
            with connection() as conn:
                conn.execute('SELECT 1 FROM video_jobs LIMIT 1')
        except Exception:
            raise HTTPException(503, 'Database unavailable') from None
        return {'status': 'ready'}

    return app

app = create_app()
