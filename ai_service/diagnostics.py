"""Safe operational logs: never serialize requests, credentials or provider errors."""
from contextvars import ContextVar
import json
import logging
import traceback

request_id = ContextVar("request_id", default="unknown")
logger = logging.getLogger("uvicorn.error.audigo")


def record(event: str, *, level: int = logging.INFO, **fields) -> None:
    logger.log(level, json.dumps({"event": event, "request_id": request_id.get(), **fields}, ensure_ascii=False))


def failure(event: str, exc: Exception, **fields) -> None:
    # Stack locations are useful; exception text/chained HTTP errors can leak keys.
    frames = [{"file": frame.filename.rsplit("/", 1)[-1], "line": frame.lineno,
               "function": frame.name} for frame in traceback.extract_tb(exc.__traceback__)]
    record(event, level=logging.ERROR, error_type=type(exc).__name__, code=getattr(exc, "code", "internal_server_error"),
           reason=getattr(exc, "reason", None), detail=getattr(exc, "detail", None) or None,
           frames=frames, **fields)
