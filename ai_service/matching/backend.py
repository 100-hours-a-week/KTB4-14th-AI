"""Synchronous adapter for the backend MatchingCandidateAiRequest contract."""
import asyncio
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ai_service.core.errors import ServiceUnavailable
from ai_service.matching.schemas import (
    MatchingCandidate, MatchingRecommendationRequest
)
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
    user_id: Id
    nickname: str | None = Field(default=None, max_length=20)
    gender: Gender | None = None
    theme: list[Theme] = Field(default_factory=list, max_length=4)
    pace: Pace | None = None

    @field_validator('theme', mode='before')
    @classmethod
    def normalize_null_themes(cls, values):
        return [] if values is None else values

    @field_validator('theme')
    @classmethod
    def validate_themes(cls, values):
        return unique_themes(values)


class BackendMatchingRequest(StrictModel):
    request: BackendMatchingCondition
    candidates: list[BackendCandidateProfile] = Field(default_factory=list, max_length=10000)

    @field_validator('candidates', mode='before')
    @classmethod
    def normalize_null_candidates(cls, values):
        return [] if values is None else values

    @model_validator(mode='after')
    def validate_unique_candidates(self):
        ids=[candidate.user_id for candidate in self.candidates]
        if len(ids)!=len(set(ids)):
            raise ValueError('candidate user_id must be unique')
        return self


class BackendRankingInput(MatchingRecommendationRequest):
    candidates: list[MatchingCandidate] = Field(max_length=10000)
    candidate_budget_available: bool = Field(default=False, exclude=True)


class BackendRankedCandidate(StrictModel):
    user_id: Id
    match_rate: int = Field(ge=0, le=100, strict=True,
        description='E5/조건 추천 점수를 0~100으로 표시한 값. 성공 확률이 아닙니다.')


class BackendMatchingResponse(StrictModel):
    candidates: list[BackendRankedCandidate]
    count: int = Field(ge=0, strict=True)


def display_match_rate(score, weights):
    # Pure cosine spans -1..1; the hybrid formula already spans 0..1.
    normalized=(score+1)/2 if tuple(weights)==(1.0,0.0,0.0) else score
    return int(round(min(1.,max(0.,normalized))*100))


def ranking_input_for_backend(body: BackendMatchingRequest, top_k: int) -> BackendRankingInput:
    condition=body.request
    # Backend profiles have no candidate budget/intro. Do not invent those
    # values or discard everyone when the requester has a budget condition.
    candidates=[MatchingCandidate(user_id=c.user_id,gender=c.gender,theme=c.theme,pace=c.pace)
                for c in body.candidates if c.gender is not None and c.pace is not None and c.theme]
    ranking=BackendRankingInput(requester_id=condition.user_id,
        preferred_companion_gender=condition.preferred_companion_gender,
        theme=condition.theme,pace=condition.pace,
        budget_min=condition.budget_min,budget_max=condition.budget_max,
        candidates=candidates,top_k=top_k)
    return ranking


async def recommend_for_backend(body, recommender, settings):
    ranking=ranking_input_for_backend(body,settings.matching_candidate_limit)
    nicknames={c.user_id:(c.nickname or '') for c in body.candidates}
    try:
        async with asyncio.timeout(settings.model_timeout_seconds):
            result=await recommender.recommend(ranking,
                tie_breaker=lambda candidate:(nicknames[candidate.user_id],candidate.user_id))
    except TimeoutError as exc:
        raise ServiceUnavailable(reason='matching_timeout') from exc
    recommendations=[BackendRankedCandidate(user_id=r.user_id,
        match_rate=display_match_rate(r.score,recommender.weights)) for r in result.data.recommendations]
    return BackendMatchingResponse(candidates=recommendations,count=len(recommendations))


BACKEND_MATCHING_EXAMPLE={
    'request':{'matching_request_id':10,'user_id':1,'preferred_companion_gender':'FEMALE',
               'theme':['NATURE','FOOD'],'pace':'BALANCED','budget_min':100000,'budget_max':800000},
    'candidates':[
        {'user_id':22,'nickname':'매칭후보A','gender':'MALE','theme':['CULTURE','SNS'],'pace':'RELAXED'},
        {'user_id':23,'nickname':'매칭후보B','gender':'FEMALE','theme':['NATURE','FOOD'],'pace':'BALANCED'},
    ],
}
