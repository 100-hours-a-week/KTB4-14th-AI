"""Synchronous adapter for the backend MatchingCandidateAiRequest contract."""
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from ai_service.core.schemas import StrictModel

Theme = Literal['NATURE','FOOD','CULTURE','REST','SNS','ACTIVITY']
Pace = Literal['RELAXED','BALANCED','PACKED']
Gender = Literal['FEMALE','MALE']
Id = Annotated[int, Field(gt=0, strict=True)]
Budget = Annotated[int, Field(ge=0, le=3000000, strict=True)]


def unique_themes(values):
    if len(values) != len(set(values)):
        raise ValueError('themes must be unique')
    return values


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
        if (self.budget_min is None) != (self.budget_max is None):
            raise ValueError('Both budgets must be supplied or omitted')
        if self.budget_min is not None and self.budget_min > self.budget_max:
            raise ValueError('budget_min must not exceed budget_max')
        return self


class BackendCandidateProfile(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    user_id: Id
    nickname: str = Field(min_length=1, max_length=500)
    profile_image_url: str | None = None
    gender: Gender
    theme: list[Theme] = Field(min_length=1, max_length=4)
    pace: Pace

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
        description='테마 일치 최대 80점 + 속도 일치 최대 20점. 성공 확률이 아닙니다.')


class BackendMatchingResponse(StrictModel):
    candidates: list[BackendRankedCandidate]
    count: int = Field(ge=0, strict=True)


BACKEND_MATCHING_EXAMPLE={
    'request':{'matching_request_id':10,'user_id':1,'preferred_companion_gender':'FEMALE',
               'theme':['NATURE','FOOD'],'pace':'BALANCED','budget_min':100000,'budget_max':800000},
    'candidates':[
        {'user_id':22,'nickname':'매칭후보A','profile_image_url':None,'gender':'MALE','theme':['CULTURE','SNS'],'pace':'RELAXED'},
        {'user_id':23,'nickname':'매칭후보B','profile_image_url':None,'gender':'FEMALE','theme':['NATURE','FOOD'],'pace':'BALANCED'},
    ],
    'limit':3,
}
