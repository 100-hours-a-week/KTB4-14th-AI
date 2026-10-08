"""백엔드가 제공한 동행자 후보를 조건 필터링 후 로컬 E5로 정렬한다."""

import asyncio
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import Field, field_validator, model_validator

from ai_service.e5_music import E5Encoder, THEME_LABELS
from ai_service.errors import ServiceUnavailable
from ai_service.schemas import NonEmpty, PACE_ALIASES, StrictModel


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
    score: float = Field(ge=-1, le=1, description="E5 코사인 유사도; 매칭 성공 확률이 아님")
    rank: int = Field(ge=1)


class MatchingRecommendationData(StrictModel):
    requester_id: int
    recommendations: list[RankedCompanion]


class MatchingRecommendationResponse(StrictModel):
    message: Literal["ai_companions_recommended"] = "ai_companions_recommended"
    data: MatchingRecommendationData


def eligible_candidates(body: MatchingRecommendationRequest) -> list[MatchingCandidate]:
    eligible = []
    has_budget = body.budget_min is not None or body.budget_max is not None
    for candidate in body.candidates:
        if candidate.user_id == body.requester_id:
            continue
        if body.preferred_companion_gender != "Any" and candidate.gender != body.preferred_companion_gender:
            continue
        if has_budget:
            # 예산 조건이 있으면 후보의 확인된 전체 예산 구간이 필요하다.
            if candidate.budget_min is None or candidate.budget_max is None:
                continue
            if body.budget_min is not None and candidate.budget_max < body.budget_min:
                continue
            if body.budget_max is not None and candidate.budget_min > body.budget_max:
                continue
        eligible.append(candidate)
    return eligible


def profile_text(profile: MatchingTravelProfile) -> str:
    themes = ", ".join(THEME_LABELS.get(theme.upper(), theme) for theme in sorted(profile.theme))
    pace = {"Relaxed": "여유롭게 쉬면서 여행", "Balanced": "관광과 휴식을 균형 있게 여행", "Packed": "많은 장소를 활발하게 여행"}[profile.pace]
    return f"여행 테마: {themes}. 여행 속도: {pace}."


class E5MatchingRecommender:
    def __init__(self, model_dir: Path, *, encoder=None):
        self.model_dir = model_dir
        self.encoder = encoder
        self._load_lock = asyncio.Lock()
        self._inference_slots = asyncio.Semaphore(2)

    async def recommend(self, body: MatchingRecommendationRequest) -> MatchingRecommendationResponse:
        candidates = eligible_candidates(body)
        recommendations = []
        if candidates:
            # 빈 후보 목록은 모델이 없어도 정상적인 빈 추천으로 반환한다.
            async with self._load_lock:
                if self.encoder is None:
                    try:
                        self.encoder = await asyncio.to_thread(E5Encoder, self.model_dir)
                    except Exception as exc:
                        raise ServiceUnavailable(reason="matching_model_load_failed") from exc
            texts = [f"query: {profile_text(body)} 취향에 맞는 여행 동행자를 찾습니다."]
            texts.extend(f"passage: {profile_text(candidate)} 소개: {candidate.introduction}" for candidate in candidates)
            try:
                async with self._inference_slots:
                    vectors = np.asarray(await asyncio.to_thread(self.encoder.encode, texts))
                if vectors.ndim != 2 or vectors.shape[0] != len(texts) or not np.isfinite(vectors).all():
                    raise ValueError("invalid embedding output")
                norms = np.linalg.norm(vectors, axis=1, keepdims=True)
                if np.any(norms <= 0):
                    raise ValueError("zero embedding vector")
                vectors = vectors / norms
                scores = np.clip(vectors[1:] @ vectors[0], -1.0, 1.0)
            except Exception as exc:
                raise ServiceUnavailable(reason="matching_model_inference_failed") from exc
            order = sorted(range(len(candidates)), key=lambda index: (-float(scores[index]), candidates[index].user_id))
            recommendations = [RankedCompanion(user_id=candidates[index].user_id, score=float(scores[index]), rank=rank)
                               for rank, index in enumerate(order[:body.top_k], 1)]
        return MatchingRecommendationResponse(data=MatchingRecommendationData(
            requester_id=body.requester_id, recommendations=recommendations,
        ))
