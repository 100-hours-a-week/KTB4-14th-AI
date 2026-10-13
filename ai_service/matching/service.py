"""백엔드가 제공한 동행자 후보를 조건 필터링 후 로컬 E5로 정렬한다."""

import asyncio
from pathlib import Path

import numpy as np

from ai_service.embeddings.e5 import E5Encoder
from ai_service.matching.catalog import LABELS as THEME_LABELS
from ai_service.core.errors import ServiceUnavailable

from ai_service.matching.schemas import (
    MatchingCandidate,
    MatchingRecommendationData,
    MatchingRecommendationRequest,
    MatchingRecommendationResponse,
    MatchingTravelProfile,
    RankedCompanion,
)


def eligible_candidates(body: MatchingRecommendationRequest) -> list[MatchingCandidate]:
    eligible = []
    has_budget = (getattr(body, 'candidate_budget_available', True)
                  and (body.budget_min is not None or body.budget_max is not None))
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
    def __init__(self, model_dir: Path, *, encoder=None, weights=(1.0, 0.0, 0.0)):
        self.model_dir = model_dir
        self.encoder = encoder
        self._load_lock = asyncio.Lock()
        self._inference_slots = asyncio.Semaphore(2)
        if len(weights) != 3 or any(not np.isfinite(w) or w < 0 for w in weights) or not np.isclose(sum(weights), 1):
            raise ValueError('Matching weights must be nonnegative and sum to one')
        self.weights = weights

    @staticmethod
    def combine_scores(body, candidates, cosine_scores, weights):
        if weights == (1.0, 0.0, 0.0):
            return cosine_scores  # Preserve the legacy cosine score contract.
        query_themes = set(body.theme)
        pace_order = {'Relaxed': 0, 'Balanced': 1, 'Packed': 2}
        themes = np.asarray([len(query_themes & set(c.theme)) / len(query_themes | set(c.theme)) for c in candidates])
        pace = np.asarray([1 - abs(pace_order[body.pace] - pace_order[c.pace]) / 2 for c in candidates])
        return weights[0] * (cosine_scores + 1) / 2 + weights[1] * themes + weights[2] * pace

    async def cosine_scores(self, body, candidates):
        async with self._load_lock:
            if self.encoder is None:
                try:
                    self.encoder = await asyncio.to_thread(E5Encoder, self.model_dir)
                except Exception as exc:
                    raise ServiceUnavailable(reason='matching_model_load_failed') from exc
        query = f'query: {profile_text(body)} 취향에 맞는 여행 동행자를 찾습니다.'
        parts = []
        try:
            async with self._inference_slots:
                for offset in range(0, len(candidates), 200):
                    batch = candidates[offset:offset + 200]
                    texts = [query] + [f'passage: {profile_text(c)} 소개: {c.introduction}' for c in batch]
                    vectors = np.asarray(await asyncio.to_thread(self.encoder.encode, texts))
                    if vectors.ndim != 2 or vectors.shape[0] != len(texts) or not np.isfinite(vectors).all():
                        raise ValueError('invalid embedding output')
                    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
                    if np.any(norms <= 0):
                        raise ValueError('zero embedding vector')
                    vectors = vectors / norms
                    parts.append(np.clip(vectors[1:] @ vectors[0], -1.0, 1.0))
        except Exception as exc:
            raise ServiceUnavailable(reason='matching_model_inference_failed') from exc
        return np.concatenate(parts) if parts else np.asarray([])

    async def recommend(self, body: MatchingRecommendationRequest, *, tie_breaker=None) -> MatchingRecommendationResponse:
        candidates = eligible_candidates(body)
        recommendations = []
        if candidates:
            # 빈 후보 목록은 모델이 없어도 정상적인 빈 추천으로 반환한다.
            cosine = await self.cosine_scores(body, candidates)
            scores = self.combine_scores(body, candidates, cosine, self.weights)
            tie_breaker = tie_breaker or (lambda candidate: candidate.user_id)
            order = sorted(range(len(candidates)), key=lambda index: (-float(scores[index]), tie_breaker(candidates[index])))
            recommendations = [RankedCompanion(user_id=candidates[index].user_id, score=float(scores[index]), rank=rank)
                               for rank, index in enumerate(order[:body.top_k], 1)]
        return MatchingRecommendationResponse(data=MatchingRecommendationData(
            requester_id=body.requester_id, recommendations=recommendations,
        ))
