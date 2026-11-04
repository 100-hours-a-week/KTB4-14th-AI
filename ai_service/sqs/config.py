"""백엔드 travel-generation-sqs-contract의 환경변수와 수신 기본값."""
from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import dotenv_values

from ai_service.core.config import ENV_FILE


@dataclass(frozen=True)
class SqsSettings:
    region: str = "ap-northeast-2"
    job_queue_url: str | None = field(default=None, repr=False)
    result_queue_url: str | None = field(default=None, repr=False)
    endpoint_url: str | None = field(default=None, repr=False)
    wait_time_seconds: int = 20
    visibility_timeout: int = 960

    @classmethod
    def from_env(cls, env_file: Path = ENV_FILE):
        values = {**dotenv_values(env_file), **os.environ}
        return cls(
            region=values.get("AWS_REGION") or "ap-northeast-2",
            job_queue_url=values.get("AUDIGO_AI_JOB_QUEUE_URL") or None,
            result_queue_url=values.get("AUDIGO_AI_RESULT_QUEUE_URL") or None,
            endpoint_url=values.get("AUDIGO_SQS_ENDPOINT") or None,
        )

    def require_configured(self):
        if not self.job_queue_url or not self.result_queue_url:
            raise ValueError("AUDIGO_AI_JOB_QUEUE_URL과 AUDIGO_AI_RESULT_QUEUE_URL이 필요합니다.")
        if self.job_queue_url == self.result_queue_url:
            raise ValueError("요청 큐와 결과 큐는 서로 달라야 합니다.")
        if any(url.endswith(".fifo") for url in (self.job_queue_url, self.result_queue_url)):
            raise ValueError("백엔드 계약은 Standard Queue를 사용합니다.")
        if not 1 <= self.wait_time_seconds <= 20 or not 1 <= self.visibility_timeout <= 43200:
            raise ValueError("SQS polling 또는 visibility 설정이 올바르지 않습니다.")
