from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, Field

from ai_service.matching import MatchingCandidate, MatchingRecommendationRequest
from ai_service.schemas import NonEmpty, StrictModel

JobType = Literal['ITINERARY_GENERATION', 'TRAVELER_MATCHING']
JobState = Literal['QUEUED', 'RUNNING', 'RETRYING', 'COMPLETED', 'FAILED']


class JobMatchingCandidate(MatchingCandidate):
    theme: list[NonEmpty] = Field(min_length=1, max_length=10,
                            validation_alias=AliasChoices('themes', 'theme'), serialization_alias='themes')
    gender: Literal['Female', 'Male', 'Other'] | None = Field(
        default=None, description='ANY는 생략 가능. 성별 조건이 있으면 미확인 후보는 제외합니다.')


class MatchingJobRequest(MatchingRecommendationRequest):
    matching_request_id: int = Field(gt=0, strict=True)
    travel_plan_id: int = Field(gt=0, strict=True)
    theme: list[NonEmpty] = Field(min_length=1, max_length=10,
                            validation_alias=AliasChoices('themes', 'theme'), serialization_alias='themes')
    candidates: list[JobMatchingCandidate] = Field(max_length=200)
    top_k: int = Field(default=10, ge=1, le=20, strict=True,
                       validation_alias=AliasChoices('limit', 'top_k'), serialization_alias='limit')


class MatchingDatabaseJobRequest(StrictModel):
    """Identifiers only: the worker loads preferences and candidates from MySQL."""

    matching_request_id: int = Field(gt=0, strict=True)
    travel_plan_id: int | None = Field(default=None, gt=0, strict=True,
        description='이전 여행 연계 SQL 매핑용 선택 값. 현재 백엔드 매칭은 여행과 연결되지 않습니다.')
    requester_id: int = Field(gt=0, strict=True)
    top_k: int = Field(default=3, ge=1, le=20, strict=True,
                       validation_alias=AliasChoices('limit', 'top_k'), serialization_alias='limit')


class DatabaseMatchingRecommendationRequest(MatchingJobRequest):
    """Internal paged DB result; the public candidate-list API keeps its 200 cap."""

    candidates: list[JobMatchingCandidate] = Field(max_length=10000)
    travel_plan_id: int | None = Field(default=None, gt=0, strict=True)
    candidate_budget_available: bool = Field(default=True, exclude=True)


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
    matching_request_id: int | None = None
    result: dict | None = None
    error: JobError | None = None
