from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import dotenv_values


ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


@dataclass(frozen=True)
class Settings:
    """외부 API 키와 생성·연결 제한 시간을 담는 설정."""
    api_token: str | None = field(default=None, repr=False)
    openai_api_key: str | None = field(default=None, repr=False)
    kakao_rest_api_key: str | None = field(default=None, repr=False)
    openai_model: str = "gpt-4o-mini"
    model_timeout_seconds: float = 60.0
    kakao_timeout_seconds: float = 10.0
    # 네 단계 전체 생성이 5분을 넘으면 실패 처리한다.
    generation_timeout_seconds: float = 300.0
    routing_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env_file: Path = ENV_FILE) -> "Settings":
        # 프로젝트 .env만 읽고 같은 이름의 환경변수가 있으면 환경변수를 우선한다.
        values = {**dotenv_values(env_file), **os.environ}
        token = values.get("AUDIGO_API_TOKEN")
        if token and len(token) < 32:
            raise RuntimeError("AUDIGO_API_TOKEN must be at least 32 characters")
        return cls(
            api_token=token,
            openai_api_key=values.get("OPENAI_API_KEY") or values.get("OPEN_API_KEY"),
            kakao_rest_api_key=values.get("KAKAO_REST_API_KEY"),
            openai_model=values.get("OPENAI_MODEL") or "gpt-4o-mini",
        )
