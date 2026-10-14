"""백엔드가 전달한 후보의 조건을 확인하고 계산식 점수로 정렬한다."""
from ai_service.matching.backend import (
    BackendMatchingRequest, BackendMatchingResponse, BackendRankedCandidate,
)
from ai_service.matching.scoring import match_rate

RANKING_VERSION = 'theme-pace-v1'


def rank_candidates(body: BackendMatchingRequest):
    condition = body.request
    ranked = []
    for candidate in body.candidates:
        if candidate.user_id == condition.user_id:
            continue
        if (condition.preferred_companion_gender != 'ANY'
                and candidate.gender != condition.preferred_companion_gender):
            continue
        rate = match_rate(condition.theme, candidate.theme, condition.pace, candidate.pace)
        if rate is not None:
            ranked.append((rate, candidate))
    ranked.sort(key=lambda item: (-item[0], item[1].nickname, item[1].user_id))
    return ranked


def recommend(body: BackendMatchingRequest) -> BackendMatchingResponse:
    selected = rank_candidates(body)[:body.limit]
    candidates = [BackendRankedCandidate(user_id=c.user_id, match_rate=rate) for rate,c in selected]
    return BackendMatchingResponse(candidates=candidates, count=len(candidates))
