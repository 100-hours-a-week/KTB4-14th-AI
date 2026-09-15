from __future__ import annotations

from datetime import date, time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Destination(StrictModel):
    provider_place_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class Trip(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    destination: Destination
    start_date: date
    end_date: date
    daily_start_time: time
    daily_end_time: time
    travelers: int = Field(ge=1, le=30)
    companion_type: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dates_and_times(self) -> "Trip":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if (self.end_date - self.start_date).days > 7:
            raise ValueError("a trip can span at most 8 calendar dates")
        if self.daily_end_time <= self.daily_start_time:
            raise ValueError("daily_end_time must be after daily_start_time")
        return self


class Preferences(StrictModel):
    themes: list[str] = Field(min_length=1, max_length=3)
    pace: str = Field(min_length=1)
    transport_modes: list[str] = Field(min_length=1)
    budget_min_krw: int | None = Field(default=None, ge=0)
    budget_max_krw: int | None = Field(default=None, ge=0)
    distance_preference: float | None = Field(default=None, ge=0, le=1)
    cuisine_types: list[str] | None = None

    @model_validator(mode="after")
    def validate_budget(self) -> "Preferences":
        if (
            self.budget_min_krw is not None
            and self.budget_max_krw is not None
            and self.budget_max_krw < self.budget_min_krw
        ):
            raise ValueError("budget_max_krw must be at least budget_min_krw")
        return self


class Constraints(StrictModel):
    must_visit_place_ids: list[str] = Field(default_factory=list)
    excluded_place_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_place_sets(self) -> "Constraints":
        overlap = set(self.must_visit_place_ids) & set(self.excluded_place_ids)
        if overlap:
            raise ValueError("the same place cannot be required and excluded")
        return self


class ItineraryRequest(StrictModel):
    trip: Trip
    preferences: Preferences
    constraints: Constraints | None = None


class TripContext(StrictModel):
    destination: str = Field(min_length=1)
    season: str = Field(min_length=1)
    mood_tags: list[str] = Field(default_factory=list)


class MusicPreferences(StrictModel):
    genres: list[str] = Field(min_length=1)
    excluded_track_ids: list[str] = Field(default_factory=list)


class MusicRequest(StrictModel):
    trip_context: TripContext
    music_preferences: MusicPreferences


class Candidate(StrictModel):
    user_id: int
    pace: str = Field(min_length=1)
    themes: list[str] = Field(default_factory=list)


class MatchRequest(StrictModel):
    matching_request_id: int
    travel_plan_id: int
    requester_id: int
    preferred_companion_gender: str
    pace: str
    themes: list[str] = Field(min_length=1, max_length=3)
    candidates: list[Candidate] = Field(min_length=1)
    budget_min: int | None = Field(default=None, ge=0)
    budget_max: int | None = Field(default=None, ge=0)
    limit: int = Field(default=3, ge=1, le=100)


class ChecklistTripContext(StrictModel):
    destination: str
    start_date: date
    end_date: date
    travelers: int = Field(ge=1, le=30)
    themes: list[str] = Field(default_factory=list)


class ChecklistRequest(StrictModel):
    travel_plan_id: int
    trip_context: ChecklistTripContext


class VideoRequest(StrictModel):
    travel_plan_id: int
    title: str = Field(min_length=1, max_length=100)
    media_urls: list[HttpUrl] = Field(min_length=1)
    style: str = Field(min_length=1)
    music_track_id: str | None = None
    duration_seconds: int = Field(default=30, ge=10, le=180)


class JobType(str, Enum):
    ITINERARY_GENERATION = "ITINERARY_GENERATION"
    TRAVELER_MATCHING = "TRAVELER_MATCHING"
    TRAVEL_VIDEO_GENERATION = "TRAVEL_VIDEO_GENERATION"


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ErrorDetail(StrictModel):
    field: str
    reason: str


class ErrorInfo(StrictModel):
    code: str
    message: str
    retryable: bool
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(StrictModel):
    error: ErrorInfo
    request_id: str


class MusicTrack(StrictModel):
    provider: str
    provider_track_id: str
    title: str
    artist: str
    preview_url: str | None


class MusicResponse(StrictModel):
    track: MusicTrack
    reason: str
    model_version: str


class ChecklistItem(StrictModel):
    content: str
    is_completed: bool
    is_checked: bool
    sort_order: int = Field(ge=1)


class ChecklistResponse(StrictModel):
    travel_plan_id: int
    title: str
    items: list[ChecklistItem]
    model_version: str


class JobAccepted(StrictModel):
    job_id: str
    job_type: JobType
    status: Literal["QUEUED"]
    status_url: str


class JobResponse(StrictModel):
    job_id: str
    job_type: JobType
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    result: dict[str, Any] | None
    error: ErrorInfo | None
    model_version: str | None
    pipeline_version: str
    completed_at: str | None
