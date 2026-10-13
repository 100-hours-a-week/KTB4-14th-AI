"""동행자 추천 조건·후보·추천 결과의 데이터 계약."""
from typing import Literal
from pydantic import Field, field_validator, model_validator

from ai_service.core.schemas import NonEmpty, PACE_ALIASES, StrictModel

class MatchingTravelProfile(StrictModel):
    theme: list[NonEmpty] = Field(min_length=1, max_length=10)
    pace: Literal["Relaxed", "Balanced", "Packed"]
    budget_min: int | None = Field(default=None, ge=0, strict=True)
    budget_max: int | None = Field(default=None, ge=0, strict=True)

    @field_validator("theme")
    @classmethod
    def normalize_themes(cls, values):
        return list(dict.fromkeys(value.lower() for value in values))

    @field_validator("pace", mode="before")
    @classmethod
    def normalize_pace(cls, value):
        if isinstance(value, str):
            canonical = PACE_ALIASES.get(value.strip().upper())
            return {"RELAXED": "Relaxed", "BALANCED": "Balanced", "PACKED": "Packed"}.get(canonical, value.strip())
        return value

    @model_validator(mode="after")
    def validate_budget(self):
        if self.budget_min is not None and self.budget_max is not None:
            if self.budget_min > self.budget_max:
                raise ValueError("budget_max must be at least budget_min")
        return self


class MatchingCandidate(MatchingTravelProfile):
    user_id: int = Field(gt=0, strict=True)
    gender: Literal["Female", "Male", "Other"]
    introduction: str = Field(default="", max_length=2000)

    @field_validator("gender", mode="before")
    @classmethod
    def normalize_gender(cls, value):
        return value.strip().title() if isinstance(value, str) else value


class MatchingRequestCreate(MatchingTravelProfile):
    """풀스택에서 보내는 매칭 조건만 입력받는다."""

    preferred_companion_gender: Literal["Female", "Male", "Other", "Any"]

    @field_validator("preferred_companion_gender", mode="before")
    @classmethod
    def normalize_gender(cls, value):
        return value.strip().title() if isinstance(value, str) else value


class MatchingRecommendationRequest(MatchingRequestCreate):
    """서버가 요청자·후보를 채워 E5에 전달하는 내부 입력."""

    requester_id: int = Field(gt=0, strict=True)
    candidates: list[MatchingCandidate] = Field(max_length=200)
    top_k: int = Field(default=5, ge=1, le=20, strict=True)

    @model_validator(mode="after")
    def validate_candidates(self):
        ids = [candidate.user_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate user_id must be unique")
        return self


class RankedCompanion(StrictModel):
    user_id: int
    score: float = Field(ge=-1, le=1, description="E5 유사도 또는 설정된 조합 추천 점수; 매칭 성공 확률이 아님")
    rank: int = Field(ge=1)


class MatchingRecommendationData(StrictModel):
    requester_id: int
    recommendations: list[RankedCompanion]


class MatchingRecommendationResponse(StrictModel):
    message: Literal["ai_companions_recommended"] = "ai_companions_recommended"
    data: MatchingRecommendationData
