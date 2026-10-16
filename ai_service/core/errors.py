"""HTTP 생성 흐름에서 사용하는 API 오류 형식.

code/message는 외부 응답, reason/detail은 운영 로그에 사용한다.
운영 정보에는 API 키·외부 응답 본문·사용자 자유 입력을 넣지 않는다.
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
    """외부 서비스나 제한 시간 문제로 재시도 시 성공할 수 있는 오류."""

    def __init__(self, *, reason: str | None = None, detail: dict | None = None) -> None:
        super().__init__(
            503, "ai_service_unavailable", "잠시 후 다시 시도해주세요.",
            reason=reason, detail=detail,
        )


class GenerationFailed(ApiError):
    """현재 요청 조건으로 유효한 일정을 만들 수 없을 때의 오류."""

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
    """모델 재생성에 사용할 내부 검증 피드백.

    클라이언트에는 직접 전달하지 않고 재시도 후에도 실패하면 GenerationFailed로 바꾼다.
    """


class MusicRecommendationFailed(ApiError):
    def __init__(self, *, reason: str | None = None, detail: dict | None = None) -> None:
        super().__init__(
            422,
            "ai_music_recommendation_failed",
            "추천 가능한 음악을 선택하지 못했습니다.",
            reason=reason, detail=detail,
        )
