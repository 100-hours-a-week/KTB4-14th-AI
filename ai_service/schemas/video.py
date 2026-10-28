"""영상 생성 서비스와 명령행 도구에서 사용하는 입력 형식과 검증 조건을 정의한다."""
from pathlib import Path
from pydantic import Field
from ai_service.schemas.common import StrictModel


DEFAULT_MOTION = (
    "Cinematic travel footage. Natural, continuous motion connecting the first and last frames. "
    "Gentle camera movement, realistic lighting, preserve people and landmarks."
)


class VideoRequest(StrictModel):
    photos: list[Path] = Field(min_length=2, max_length=91)
    duration_seconds: int = Field(ge=10, le=180, strict=True)
    prompt: str = Field(default=DEFAULT_MOTION, min_length=1, max_length=2000)
    seed: int = Field(default=0, ge=0, le=2**63 - 1)


class ClipRequest(StrictModel):
    """사진 두 장으로 단일 GPU 클립을 검증하며 최종 영상의 최소 10초 조건과 구분한다."""
    photos: list[Path] = Field(min_length=2, max_length=2)
    duration_seconds: int = Field(default=5, ge=1, le=5, strict=True)
    prompt: str = Field(default=DEFAULT_MOTION, min_length=1, max_length=2000)
    seed: int = Field(default=0, ge=0, le=2**63 - 1)
