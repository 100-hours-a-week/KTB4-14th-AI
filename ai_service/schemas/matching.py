"""동행자 매칭의 조건·후보·요청·응답 스키마."""
from typing import Annotated, Literal
from pydantic import ConfigDict, Field, field_validator, model_validator
from ai_service.schemas.common import StrictModel


Theme = Literal['NATURE','FOOD','CULTURE','REST','SNS','ACTIVITY']
Pace = Literal['RELAXED','BALANCED','PACKED']
Gender = Literal['FEMALE','MALE']
Id = Annotated[int, Field(gt=0, strict=True)]
Budget = Annotated[int, Field(ge=0, le=3000000, strict=True)]


def unique_themes(values):
    if len(values) != len(set(values)):
        raise ValueError('themes must be unique')
    return values


def validate_budget_range(budget_min, budget_max):
    if (budget_min is None) != (budget_max is None):
        raise ValueError('Both budgets must be supplied or omitted')
    if budget_min is not None and budget_min > budget_max:
        raise ValueError('budget_min must not exceed budget_max')


class BackendMatchingCondition(StrictModel):
    matching_request_id: Id
    user_id: Id
    preferred_companion_gender: Literal['ANY','FEMALE','MALE']
    theme: list[Theme] = Field(min_length=1, max_length=3)
    pace: Pace
    budget_min: Budget | None = None
    budget_max: Budget | None = None

    @field_validator('theme')
    @classmethod
    def validate_themes(cls, values):
        return unique_themes(values)

    @model_validator(mode='after')
    def validate_budget(self):
        validate_budget_range(self.budget_min, self.budget_max)
        return self


class BackendCandidateProfile(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    user_id: Id
    nickname: str = Field(min_length=1, max_length=500)
    profile_image_url: str | None = None
    gender: Gender
    theme: list[Theme] = Field(min_length=1, max_length=4)
    pace: Pace
    budget_min: Budget | None = Field(default=None,
        description='후보의 최소 예산. 최대 예산과 함께 전달하며 둘 다 생략할 수 있습니다.')
    budget_max: Budget | None = Field(default=None,
        description='후보의 최대 예산. 요청 예산과 비교해 최대 5점만 감점하며 후보 제외에는 사용하지 않습니다.')

    @model_validator(mode='after')
    def validate_budget(self):
        validate_budget_range(self.budget_min, self.budget_max)
        return self

    @field_validator('nickname')
    @classmethod
    def reject_blank_nickname(cls, value):
        if not value.strip():
            raise ValueError('nickname must not be blank')
        return value

    @field_validator('theme')
    @classmethod
    def validate_themes(cls, values):
        return unique_themes(values)


class BackendMatchingRequest(StrictModel):
    request: BackendMatchingCondition
    candidates: list[BackendCandidateProfile] = Field(max_length=10000)
    limit: int = Field(default=3, ge=1, le=3, strict=True,
        description='추천할 최대 후보 수. 생략하면 3명이며 1~3만 허용합니다.')

    @model_validator(mode='after')
    def validate_unique_candidates(self):
        ids=[candidate.user_id for candidate in self.candidates]
        if len(ids)!=len(set(ids)):
            raise ValueError('candidate user_id must be unique')
        return self


class BackendRankedCandidate(StrictModel):
    user_id: Id
    match_rate: int = Field(ge=0, le=100, strict=True,
        description='테마 최대 80점 + 속도 최대 20점에서 예산 불일치로 최대 5점 감점 후 반올림합니다. 요청·후보 어느 한쪽 예산이 없으면 감점하지 않습니다. 성공 확률이 아닙니다.')


class BackendMatchingResponse(StrictModel):
    candidates: list[BackendRankedCandidate]
    count: int = Field(ge=0, strict=True)


class MatchingError(StrictModel):
    code: str
    message: str
    retryable: bool = False
    details: list[dict] = Field(default_factory=list)


class MatchingErrorResponse(StrictModel):
    error: MatchingError
    request_id: str
