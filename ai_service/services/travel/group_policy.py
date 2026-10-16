"""공개 API를 바꾸지 않고 인원별 일정·경로 기준을 제공한다."""


def movement_buffer_minutes(headcount: int) -> int:
    """실제 경로 시간과 별도로 일정 배치에만 더할 단체 이동 여유시간."""
    if headcount >= 20:
        return 10
    if headcount >= 10:
        return 5
    return 0


def daily_item_reduction(headcount: int) -> int:
    if headcount >= 20:
        return 2
    if headcount >= 10:
        return 1
    return 0


def proximity_floor(headcount: int) -> float:
    """거리 선호도가 낮더라도 그룹에 적용할 가까운 후보의 최소 비율."""
    if headcount >= 20:
        return 0.65
    if headcount >= 10:
        return 0.40
    if headcount >= 5:
        return 0.15
    return 0.0


def transfer_penalty_seconds(headcount: int) -> int:
    """카카오의 실제 소요시간은 그대로 두고 경로 선택 점수에만 적용한다."""
    if headcount >= 20:
        return 12 * 60
    if headcount >= 10:
        return 5 * 60
    if headcount >= 5:
        return 90
    return 0
