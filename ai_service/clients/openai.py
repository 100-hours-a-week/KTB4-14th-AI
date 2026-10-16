"""OpenAI JSON Schema 요청과 응답 검증."""
from __future__ import annotations
import httpx
from pydantic import ValidationError
from ai_service.core.config import Settings
from ai_service.core.errors import GenerationFailed, InvalidModelOutput, ServiceUnavailable


class OpenAIClient:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings

    async def complete(self, messages: list[dict], schema: dict, name: str) -> str:
        """지정한 JSON Schema에 맞는 모델 응답 문자열을 가져온다."""
        if not self.settings.openai_api_key:
            raise ServiceUnavailable(reason="openai_api_key_missing")
        try:
            response = await self.client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                timeout=self.settings.model_timeout_seconds,
                json={
                    "model": self.settings.openai_model,
                    "messages": messages,
                    "max_completion_tokens": 8000,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": name,
                            "strict": True,
                            "schema": schema,
                        },
                    },
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # 외부 응답 본문·인증정보·헤더를 호출자에게 전달하지 않는다.
            raise ServiceUnavailable(reason="openai_request_failed", detail={"error": type(exc).__name__, "http_status": getattr(getattr(exc, "response", None), "status_code", None)}) from exc
        try:
            choice = response.json()["choices"][0]
            if not isinstance(choice, dict) or not isinstance(
                choice.get("message"), dict
            ):
                raise InvalidModelOutput("invalid model response envelope")
            if choice["message"].get("refusal"):
                raise GenerationFailed("요청한 조건으로 일정을 생성할 수 없습니다.", reason="openai_refusal")
            if choice.get("finish_reason") != "stop":
                raise InvalidModelOutput("model response is incomplete")
            raw = choice["message"]["content"]
            if not isinstance(raw, str):
                raise InvalidModelOutput("model content must be JSON text")
            return raw
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            # 원본 출력에 사용자 입력이 섞일 수 있어 그대로 노출하지 않는다.
            raise InvalidModelOutput(
                "model output must be complete JSON matching the schema"
            ) from exc
