"""기존 JSON·큐 API 오류 응답 계약을 유지하는 예외 핸들러."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_service.core.errors import ApiError
from ai_service.core.diagnostics import failure, record
from ai_service.jobs.errors import is_job_request, error_response as job_error_response


def register_exception_handlers(app: FastAPI):
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        failure("request_failed", exc, http_status=exc.status_code)
        if is_job_request(request):
            return JSONResponse(status_code=exc.status_code, content=job_error_response(
                request, exc.code.upper(), exc.message,
                retryable=getattr(exc, 'retryable', exc.status_code == 503),
                details=getattr(exc, 'details', []),
            ))
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
        if is_job_request(request):
            return JSONResponse(status_code=400, content=job_error_response(
                request, 'INVALID_REQUEST', '입력값을 확인해 주세요.', details=[{
                    'field': '.'.join(map(str, error['loc'])), 'type': error['type'],
                } for error in exc.errors()[:10]],
            ))
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
        if is_job_request(request):
            return JSONResponse(status_code=exc.status_code, headers=exc.headers, content=job_error_response(
                request, {401: 'UNAUTHORIZED', 404: 'JOB_NOT_FOUND', 503: 'QUEUE_UNAVAILABLE'}.get(exc.status_code, 'HTTP_ERROR'),
                {401: '인증 정보를 확인해 주세요.', 404: '작업을 찾을 수 없습니다.'}.get(exc.status_code, '요청을 처리할 수 없습니다.'),
                retryable=exc.status_code == 503,
            ))
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
        if is_job_request(request):
            return JSONResponse(status_code=500, content=job_error_response(
                request, 'INTERNAL_SERVER_ERROR', '서버 내부 오류가 발생했습니다.',
            ))
        return JSONResponse(
            status_code=500,
            content={"message": "internal_server_error", "data": None},
            headers={"X-Request-Id": getattr(request.state, "request_id", "unknown")},
        )
