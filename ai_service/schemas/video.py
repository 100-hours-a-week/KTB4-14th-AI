"""영상 생성 서비스와 명령행 도구에서 사용하는 입력 형식과 검증 조건을 정의한다."""
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from ai_service.schemas.common import StrictModel


DEFAULT_MOTION = (
    "Recreate the referenced trip as immersive live-action footage filmed by a traveler returning there. "
    "Animate the real environment and subjects with physical motion and changing viewpoint. "
    "Use photos as location references, not frozen pictures to display or dissolve between."
)


class MusicSelection(StrictModel):
    preset: Literal['AUTO', 'WARM', 'BRIGHT', 'CALM', 'NONE'] = 'AUTO'
    file: Path | None = None
    volume: float = Field(default=0.65, gt=0, le=1)
    start_seconds: float = Field(default=0, ge=0, le=86400)

    @model_validator(mode='after')
    def validate_choice(self):
        if self.preset == 'NONE' and self.file is not None:
            raise ValueError('music NONE cannot be combined with a file')
        return self


class MemoryOptions(StrictModel):
    prompt: str = Field(default=DEFAULT_MOTION, min_length=1, max_length=2000)
    seed: int = Field(default=0, ge=0, le=2**63 - 1)
    memory_style: Literal['WARM', 'LIVELY', 'CINEMATIC'] = 'WARM'
    memory_context: str = Field(default='', max_length=500)
    music: MusicSelection = Field(default_factory=MusicSelection)
    photo_fit: Literal['blurred_background', 'letterbox'] = 'blurred_background'
    quality_policy: Literal['WARN', 'REJECT'] = 'WARN'
    story_mode: Literal['MONTAGE', 'TRANSITION', 'PHOTO_MOTION'] = 'TRANSITION'


class VideoRequest(MemoryOptions):
    photos: list[Path] = Field(min_length=2, max_length=120)
    duration_seconds: int = Field(ge=10, le=180, strict=True)

    @model_validator(mode='after')
    def validate_photo_capacity(self):
        if self.story_mode != 'PHOTO_MOTION' and len(self.photos) > 91:
            raise ValueError('AI video supports at most 91 photos')
        return self


class ClipRequest(MemoryOptions):
    """사진 두 장으로 최대 5초 샘플을 검증하며 10초 이상 최종 영상과 구분한다."""
    photos: list[Path] = Field(min_length=2, max_length=2)
    duration_seconds: int = Field(default=5, ge=1, le=5, strict=True)


class TravelShot(StrictModel):
    source_index: int = Field(ge=0, strict=True)
    end_source_index: int | None = Field(default=None, ge=0, strict=True)
    people_visible: bool | None = None
    duration_seconds: int | None = Field(default=None, ge=1, le=30, strict=True)
    memory_reason: str | None = Field(default=None, min_length=5, max_length=300)
    visible_scene: str = Field(min_length=10, max_length=700)
    subject_action: str = Field(min_length=10, max_length=700)
    camera_path: str = Field(min_length=10, max_length=700)


class TravelStoryboard(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=40)
    closing_line: str | None = Field(default=None, min_length=1, max_length=60)
    emotional_arc: str | None = Field(default=None, min_length=10, max_length=500)
    music_direction: str | None = Field(default=None, min_length=10, max_length=500)
    narrative: str = Field(min_length=30, max_length=1800)
    shots: list[TravelShot] = Field(min_length=1, max_length=8)


class MemoryVideoRequest(MemoryOptions):
    """대표 사진을 선별해 단일 LTX 키프레임 영상으로 생성한다."""
    photos: list[Path] = Field(min_length=2, max_length=1000)
    story_mode: Literal['TRANSITION'] = 'TRANSITION'
    backend: Literal['ltx23', 'ltx23_film', 'wan_segments'] = 'ltx23_film'
    max_selected_photos: int = Field(default=12, ge=2, le=37, strict=True)
    duration_seconds: int = Field(default=30, ge=5, le=30, strict=True)
    transition_seconds: int = Field(default=5, ge=2, le=5, strict=True)
    max_duration_seconds: int = Field(default=30, ge=5, le=180, strict=True)
    reference_strength: float = Field(default=0.7, ge=0.1, le=1)
    photo_fit: Literal['cinematic_crop','blurred_background','letterbox'] = 'cinematic_crop'
    storyboard: TravelStoryboard | None = None
    preview_only: bool = False

    @model_validator(mode='after')
    def validate_backend_capacity(self):
        if self.backend == 'ltx23' and (self.max_duration_seconds > 30 or self.duration_seconds > self.max_duration_seconds or self.max_selected_photos > 12):
            raise ValueError('LTX trial supports at most 30 seconds and 12 scene references')
        return self
