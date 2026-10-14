"""테마 80점·여행 속도 20점으로 계산하는 동행자 점수."""
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
