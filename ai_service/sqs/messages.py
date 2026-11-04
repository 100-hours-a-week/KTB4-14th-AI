"""BE feature/sqs-integration의 Job/Result 메시지 v1 계약."""
from datetime import datetime, timezone
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Stage = Literal["PLACE_RECOMMEND", "STAY_RECOMMEND", "ROUTE_OPTIMIZE", "MUSIC_RECOMMEND"]
State = Literal["PENDING", "RUNNING", "DONE", "FAILED"]


class Envelope(BaseModel):
    # Java의 @JsonIgnoreProperties(ignoreUnknown=true)와 동일한 확장 정책.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    event_id: str = Field(min_length=1)
    generation_job_id: int = Field(gt=0, strict=True)
    message_version: Literal["1"]
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_instant(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at은 시간대가 있는 Instant여야 합니다.")
        return value


class JobMessage(Envelope):
    # 요청 DTO 오류도 올바른 generation_job_id로 FAILED를 보낼 수 있도록
    # envelope를 먼저 검증하고 기존 TravelGenerationRequest는 워커에서 검증한다.
    request: dict


class ResultMessage(Envelope):
    type: Literal["PROGRESS", "COMPLETED", "FAILED"]
    stage: Stage | None = None
    state: State | None = None
    payload: dict | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_content(self):
        if self.type == "PROGRESS" and (self.stage is None or self.state is None):
            raise ValueError("PROGRESS에는 stage와 state가 필요합니다.")
        if self.type == "COMPLETED" and self.payload is None:
            raise ValueError("COMPLETED에는 payload가 필요합니다.")
        if self.type == "FAILED" and not self.error_message:
            raise ValueError("FAILED에는 error_message가 필요합니다.")
        return self


def result_message(job: JobMessage, kind: str, **values) -> ResultMessage:
    # 동일 작업의 동일 이벤트는 재수신·재발행에도 같은 ID를 사용한다.
    # BE Result Inbox가 전송 응답 유실 후의 중복 발행을 식별할 수 있다.
    identity = f"audigo:sqs:1:{job.generation_job_id}:{kind}:{values.get('stage')}:{values.get('state')}"
    return ResultMessage(
        event_id=str(uuid5(NAMESPACE_URL, identity)),
        generation_job_id=job.generation_job_id,
        message_version="1", occurred_at=datetime.now(timezone.utc), type=kind, **values,
    )
