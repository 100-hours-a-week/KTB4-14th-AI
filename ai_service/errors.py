from __future__ import annotations


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or []


class JobNotFound(ApiError):
    def __init__(self) -> None:
        super().__init__(404, "JOB_NOT_FOUND", "작업을 찾을 수 없습니다.")


class IdempotencyConflict(ApiError):
    def __init__(self) -> None:
        super().__init__(
            409,
            "IDEMPOTENCY_CONFLICT",
            "같은 키에 다른 요청을 사용할 수 없습니다.",
        )


class FeatureNotConfigured(ApiError):
    def __init__(self, message: str) -> None:
        super().__init__(503, "FEATURE_NOT_CONFIGURED", message, retryable=False)

