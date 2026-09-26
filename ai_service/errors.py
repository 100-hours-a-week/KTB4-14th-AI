"""API errors shared by the HTTP handlers and the SSE pipeline.

Every ApiError carries two layers of information:

- ``code`` / ``message``: the public contract sent to the backend (unchanged wire format).
- ``reason`` / ``detail``: operator-only diagnostics written to the logs by
  ``diagnostics.failure``. ``reason`` is a stable snake_case failure code you can
  search for in CloudWatch; ``detail`` holds safe numbers/IDs (never API keys,
  provider response bodies or user free text).
"""
from __future__ import annotations


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.reason = reason
        self.detail = detail or {}


class ServiceUnavailable(ApiError):
    """An upstream (OpenAI, Kakao, YouTube) or time budget failed; retrying may succeed."""

    def __init__(self, *, reason: str | None = None, detail: dict | None = None) -> None:
        super().__init__(
            503, "ai_service_unavailable", "잠시 후 다시 시도해주세요.",
            reason=reason, detail=detail,
        )


class GenerationFailed(ApiError):
    """The request cannot be turned into a valid itinerary; retrying the same input will not help."""

    def __init__(
        self,
        message: str = "추천 가능한 여행 일정을 생성하지 못했습니다.",
        *,
        reason: str | None = None,
        detail: dict | None = None,
    ) -> None:
        super().__init__(
            422, "ai_itinerary_generation_failed", message, reason=reason, detail=detail
        )


class RoutingUnavailable(ApiError):
    def __init__(self, *, reason: str | None = None, detail: dict | None = None) -> None:
        super().__init__(
            503,
            "routing_service_unavailable",
            "길찾기 정보를 확인할 수 없습니다. 잠시 후 다시 시도해주세요.",
            reason=reason, detail=detail,
        )


class InvalidModelOutput(ValueError):
    """Safe validation feedback for one bounded model repair attempt.

    Not an ApiError: it never reaches the client directly. The pipeline sends its
    text back to the model as correction feedback, and converts it to
    GenerationFailed only after the retries are exhausted.
    """


class MusicRecommendationFailed(ApiError):
    def __init__(self, *, reason: str | None = None, detail: dict | None = None) -> None:
        super().__init__(
            422,
            "ai_music_recommendation_failed",
            "추천 가능한 음악을 선택하지 못했습니다.",
            reason=reason, detail=detail,
        )
