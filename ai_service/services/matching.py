"""백엔드가 전달한 후보의 조건을 확인하고 계산식 점수로 정렬한다."""
from fractions import Fraction

from ai_service.schemas.matching import BackendMatchingRequest, BackendMatchingResponse, BackendRankedCandidate

PACE_LEVELS = {'RELAXED': 0, 'BALANCED': 1, 'PACKED': 2}


def is_pace_compatible(request_pace, candidate_pace):
    return abs(PACE_LEVELS[request_pace] - PACE_LEVELS[candidate_pace]) < 2


def budget_compatibility(request_min, request_max, candidate_min, candidate_max):
    """두 예산 범위 중 짧은 범위가 겹치는 비율을 구한다. 미제공은 비교하지 않는다."""
    if any(value is None for value in (request_min, request_max, candidate_min, candidate_max)):
        return None
    start = max(request_min, candidate_min)
    end = min(request_max, candidate_max)
    span = min(request_max - request_min, candidate_max - candidate_min)
    # 고정 금액은 다른 쪽 범위에 포함되면 일치한다. 0원도 유효한 금액이다.
    if span == 0:
        return Fraction(int(start <= end))
    return Fraction(max(0, end - start), span)


def match_rate(request_themes, candidate_themes, request_pace, candidate_pace, *,
               request_budget_min=None, request_budget_max=None,
               candidate_budget_min=None, candidate_budget_max=None):
    gap = abs(PACE_LEVELS[request_pace] - PACE_LEVELS[candidate_pace])
    if gap == 2:
        return None
    count = len(request_themes)
    if not count:
        raise ValueError('request_themes must not be empty')
    overlap = len(set(request_themes) & set(candidate_themes))
    pace = 20 if gap == 0 else 10
    score = Fraction(80 * overlap, count) + pace
    budget = budget_compatibility(request_budget_min, request_budget_max,
                                  candidate_budget_min, candidate_budget_max)
    if budget is not None:
        score -= 5 * (1 - budget)
    # 테마와 예산을 합산한 뒤 한 번만 반올림해 부동소수점 오차를 피한다.
    return max(0, (2 * score.numerator + score.denominator) // (2 * score.denominator))


RANKING_VERSION = 'theme-pace-budget-v2'


def rank_candidates(body: BackendMatchingRequest):
    condition = body.request
    ranked = []
    for candidate in body.candidates:
        if candidate.user_id == condition.user_id:
            continue
        if (condition.preferred_companion_gender != 'ANY'
                and candidate.gender != condition.preferred_companion_gender):
            continue
        rate = match_rate(condition.theme, candidate.theme, condition.pace, candidate.pace,
                          request_budget_min=condition.budget_min,
                          request_budget_max=condition.budget_max,
                          candidate_budget_min=candidate.budget_min,
                          candidate_budget_max=candidate.budget_max)
        if rate is not None:
            ranked.append((rate, candidate))
    ranked.sort(key=lambda item: (-item[0], item[1].nickname, item[1].user_id))
    return ranked


def recommend(body: BackendMatchingRequest) -> BackendMatchingResponse:
    selected = rank_candidates(body)[:body.limit]
    candidates = [BackendRankedCandidate(user_id=c.user_id, match_rate=rate) for rate,c in selected]
    return BackendMatchingResponse(candidates=candidates, count=len(candidates))
