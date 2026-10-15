"""백엔드가 전달한 후보의 조건을 확인하고 계산식 점수로 정렬한다."""
from ai_service.schemas.matching import BackendMatchingRequest, BackendMatchingResponse, BackendRankedCandidate

PACE_LEVELS = {'RELAXED': 0, 'BALANCED': 1, 'PACKED': 2}


def is_pace_compatible(request_pace, candidate_pace):
    return abs(PACE_LEVELS[request_pace] - PACE_LEVELS[candidate_pace]) < 2


def match_rate(request_themes, candidate_themes, request_pace, candidate_pace):
    gap = abs(PACE_LEVELS[request_pace] - PACE_LEVELS[candidate_pace])
    if gap == 2:
        return None
    count = len(request_themes)
    if not count:
        raise ValueError('request_themes must not be empty')
    overlap = len(set(request_themes) & set(candidate_themes))
    pace = 20 if gap == 0 else 10
    # Round half up using integers: 80 * overlap / count + pace.
    numerator = 80 * overlap + pace * count
    return (2 * numerator + count) // (2 * count)


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
