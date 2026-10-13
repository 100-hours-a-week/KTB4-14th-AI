"""요청 본문·인증정보·외부 오류 본문을 제외한 운영 로그를 기록한다."""
from contextvars import ContextVar
import json
import logging
import traceback

request_id = ContextVar("request_id", default="unknown")
logger = logging.getLogger("uvicorn.error.audigo")


def record(event: str, *, level: int = logging.INFO, **fields) -> None:
    """현재 요청 ID를 붙여 구조화된 이벤트 로그를 남긴다."""
    logger.log(level, json.dumps({"event": event, "request_id": request_id.get(), **fields}, ensure_ascii=False))


def failure(event: str, exc: Exception, **fields) -> None:
    """예외 유형·안전한 원인·호출 위치만 실패 로그에 기록한다."""
    # 예외 문자열과 연결된 HTTP 오류에는 비밀값이 있을 수 있다.
    frames = [{"file": frame.filename.rsplit("/", 1)[-1], "line": frame.lineno,
               "function": frame.name} for frame in traceback.extract_tb(exc.__traceback__)]
    record(event, level=logging.ERROR, error_type=type(exc).__name__, code=getattr(exc, "code", "internal_server_error"),
           reason=getattr(exc, "reason", None), detail=getattr(exc, "detail", None) or None,
           frames=frames, **fields)
