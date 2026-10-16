"""기능별 스키마가 함께 사용하는 기반 모델·지역·기간·오류·별칭을 정의한다."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
import unicodedata
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

KST = ZoneInfo("Asia/Seoul")
NonEmpty = Annotated[str, Field(min_length=1, max_length=500)]
PACE_ALIASES = {
    "RELAXED": "RELAXED",
    "여유롭게": "RELAXED",
    "BALANCED": "BALANCED",
    "보통": "BALANCED",
    "PACKED": "PACKED",
    "DENSE": "PACKED",
    "TIGHT": "PACKED",
    "BUSY": "PACKED",
    "빽빽하게": "PACKED",
}
TRANSPORT_ALIASES = {
    "WALK": "WALK",
    "WALKING": "WALK",
    "도보": "WALK",
    "PUBLIC_TRANSPORT": "PUBLIC_TRANSPORT",
    "PUBLIC_TRANSIT": "PUBLIC_TRANSPORT",
    "대중교통": "PUBLIC_TRANSPORT",
    "CAR": "CAR",
    "RENTAL_CAR": "CAR",
    "자가용": "CAR",
    "렌터카": "CAR",
    "TAXI": "CAR",
}


class StrictModel(BaseModel):
    """알 수 없는 필드를 거부하는 요청·응답 모델의 공통 기반."""
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, allow_inf_nan=False
    )


class Region(StrictModel):
    region_id: int = Field(gt=0)
    full_name: NonEmpty

    @field_validator("full_name", mode="before")
    @classmethod
    def normalize_full_name(cls, value):
        # 같은 한글 표기는 정규화해 카카오 검색 결과가 달라지지 않게 한다.
        return unicodedata.normalize("NFC", value) if isinstance(value, str) else value


class Duration(StrictModel):
    """도착·출발 시각을 검증하고 한국 시간 기준 범위를 제공한다."""
    arrival_datetime: datetime = Field(
        description="여행 도착 일시. 시간대 표기가 없으면 한국시간(Asia/Seoul)으로 해석",
        examples=["2026-09-19T10:00:00"],
    )
    departure_datetime: datetime = Field(
        description="여행 출발 일시. 시간대 표기가 없으면 한국시간(Asia/Seoul)으로 해석",
        examples=["2026-09-21T18:00:00"],
    )

    @field_validator("arrival_datetime", "departure_datetime", mode="before")
    @classmethod
    def require_datetime(cls, value):
        if not isinstance(value, datetime) and not (
            isinstance(value, str) and "T" in value
        ):
            raise ValueError("ISO datetime with a date and time is required")
        return value

    @model_validator(mode="after")
    def validate_window(self):
        if bool(self.arrival_datetime.tzinfo) != bool(self.departure_datetime.tzinfo):
            raise ValueError("both datetimes must use the same timezone convention")
        if self.departure_datetime <= self.arrival_datetime:
            raise ValueError("departure_datetime must be after arrival_datetime")
        start, end = self.local_bounds()
        if (end.date() - start.date()).days > 7:
            raise ValueError("a trip can span at most 8 calendar dates")
        return self

    def local_bounds(self) -> tuple[datetime, datetime]:
        def local(value):
            return value.astimezone(KST).replace(tzinfo=None) if value.tzinfo else value

        return local(self.arrival_datetime), local(self.departure_datetime)


class ErrorResponse(StrictModel):
    message: str
    data: dict | None
