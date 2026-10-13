"""응답·로그에 공통 요청 ID를 연결한다."""
from uuid import uuid4
from fastapi import FastAPI, Request
from ai_service.core.diagnostics import request_id as log_request_id


def register_request_id(app: FastAPI):
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
