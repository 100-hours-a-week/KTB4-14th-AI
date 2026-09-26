from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ServiceUnavailable(ApiError):
    def __init__(self) -> None:
        super().__init__(503, "ai_service_unavailable", "잠시 후 다시 시도해주세요.")


class GenerationFailed(ApiError):
    def __init__(
        self, message: str = "추천 가능한 여행 일정을 생성하지 못했습니다.", *, reason: str | None = None
    ) -> None:
        super().__init__(422, "ai_itinerary_generation_failed", message)
        self.reason = reason


class RoutingUnavailable(ApiError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "routing_service_unavailable",
            "길찾기 정보를 확인할 수 없습니다. 잠시 후 다시 시도해주세요.",
        )


class InvalidModelOutput(ValueError):
    """Safe validation feedback for one bounded model repair attempt."""


class MusicRecommendationFailed(ApiError):
    def __init__(self) -> None:
        super().__init__(
            422,
            "ai_music_recommendation_failed",
            "추천 가능한 음악을 선택하지 못했습니다.",
        )
