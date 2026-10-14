from datetime import datetime
from typing import Literal

from pydantic import Field
from ai_service.core.schemas import StrictModel

JobType = Literal['ITINERARY_GENERATION']
JobState = Literal['QUEUED', 'RUNNING', 'RETRYING', 'COMPLETED', 'FAILED']


class JobAccepted(StrictModel):
    job_id: str
    job_type: JobType
    status: JobState
    status_url: str


class JobError(StrictModel):
    code: str
    message: str
    retryable: bool = False
    details: list[dict] = Field(default_factory=list)


class JobErrorResponse(StrictModel):
    error: JobError
    request_id: str


class JobStatus(JobAccepted):
    request_id: str
    current_stage: str | None = None
    stage_status: str | None = None
    attempt: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    next_retry_at: datetime | None = None
    generation_job_id: int | None = None
    travel_plan_id: int | None = None
    result: dict | None = None
    error: JobError | None = None
