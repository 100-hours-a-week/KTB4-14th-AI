"""사진 순서와 요청 길이에 따라 FLF2V로 생성할 짧은 영상 클립을 계획한다."""
from dataclasses import dataclass
import math

from ai_service.services.video.errors import VideoError


@dataclass(frozen=True)
class ClipPlan:
    start_index: int
    end_index: int
    duration_seconds: float
    frames: int
    fps: float
    kind: str


def plan_clips(photo_count: int, duration_seconds: int, *, smoke=False, story_mode='TRANSITION') -> list[ClipPlan]:
    if story_mode == 'PHOTO_MOTION':
        if photo_count < 2 or duration_seconds < photo_count * 1.5:
            raise VideoError('too_many_photos_for_duration', minimum_seconds=math.ceil(photo_count * 1.5))
        # 각 컷을 정수 프레임에 배분해 홀수 사진 수에서도 총 길이를 보존한다.
        boundaries = [round(i * duration_seconds * 16 / photo_count) for i in range(photo_count + 1)]
        return [ClipPlan(i, i, (boundaries[i+1] - boundaries[i]) / 16,
                         boundaries[i+1] - boundaries[i], 16, 'photo_motion')
                for i in range(photo_count)]
    if story_mode == 'MONTAGE':
        if photo_count < 2 or duration_seconds < photo_count * 1.5:
            raise VideoError('too_many_photos_for_duration', minimum_seconds=math.ceil(photo_count * 1.5))
        count = max(photo_count, math.ceil(duration_seconds / 5))
        per_photo, extra = divmod(count, photo_count)
        duration = duration_seconds / count
        frames = 4 * math.ceil((duration * 16 - 1) / 4) + 1
        return [ClipPlan(index, index, duration, frames, frames / duration, 'memory_shot')
                for index in range(photo_count) for _ in range(per_photo + (index < extra))]
    if story_mode != 'TRANSITION':
        raise VideoError('story_mode_invalid')
    pairs = photo_count - 1
    if pairs < 1 or (not smoke and duration_seconds < pairs * 2):
        raise VideoError("too_many_photos_for_duration", minimum_seconds=pairs * 2)
    count = 1 if smoke else max(pairs, math.ceil(duration_seconds / 5))
    per_pair, extra = divmod(count, pairs)
    plans = []
    duration = duration_seconds / count
    # Wan 시간축 조건에 맞게 4k+1 프레임을 생성하고 배정 길이에 맞춰 재생률을 조정한다.
    # 기준 16fps보다 추가되는 프레임은 4개 미만이며 모든 클립은 확산 모델로 새로 생성한다.
    frames = 4 * math.ceil((duration * 16 - 1) / 4) + 1
    for pair in range(pairs):
        for repeat in range(per_pair + (pair < extra)):
            plans.append(ClipPlan(
                pair if repeat == 0 else pair + 1, pair + 1, duration,
                frames, frames / duration, "transition" if repeat == 0 else "motion_hold",
            ))
    return plans
