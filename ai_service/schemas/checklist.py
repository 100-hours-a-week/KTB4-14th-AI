"""여행 체크리스트 생성 요청과 검증된 생성 결과."""
from datetime import date
import re
from typing import Annotated, Literal
import unicodedata

from pydantic import BeforeValidator, Field, ValidationInfo, field_validator

from ai_service.schemas.common import StrictModel


Theme = Literal['NATURE', 'FOOD', 'CULTURE', 'REST', 'SNS', 'ACTIVITY']
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
ChecklistTitle = Annotated[str, Field(min_length=1, max_length=100)]
ChecklistContent = Annotated[str, Field(min_length=1, max_length=200)]


def calendar_date(value):
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('date must use YYYY-MM-DD')
    return value


CalendarDate = Annotated[date, BeforeValidator(calendar_date)]


class TripContext(StrictModel):
    destination: str = Field(min_length=1, max_length=500)
    start_date: CalendarDate
    end_date: CalendarDate
    travelers: PositiveInt
    themes: list[Theme] = Field(min_length=1, max_length=3)

    @field_validator('end_date')
    @classmethod
    def validate_end_date(cls, value: date, info: ValidationInfo):
        start = info.data.get('start_date')
        if start is not None and value < start:
            raise ValueError('end_date must be on or after start_date')
        return value

    @field_validator('themes')
    @classmethod
    def validate_themes(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('themes must be unique')
        return values


class ChecklistRequest(StrictModel):
    travel_plan_id: PositiveInt
    trip_context: TripContext


class GeneratedChecklistItem(StrictModel):
    content: ChecklistContent


class GeneratedChecklist(StrictModel):
    """모델은 문구만 생성하고 ID·상태·정렬 순서는 서버에서 부여한다."""
    title: ChecklistTitle
    items: list[GeneratedChecklistItem] = Field(min_length=1, max_length=50)

    @field_validator('items')
    @classmethod
    def validate_unique_items(cls, values):
        contents = [unicodedata.normalize('NFKC', ' '.join(item.content.split())).casefold()
                    for item in values]
        if len(contents) != len(set(contents)):
            raise ValueError('checklist items must be unique')
        return values


class ChecklistItem(GeneratedChecklistItem):
    is_completed: Literal[False] = False
    is_checked: Literal[False] = False
    sort_order: PositiveInt


class ChecklistResponse(StrictModel):
    travel_plan_id: PositiveInt
    title: ChecklistTitle
    items: list[ChecklistItem] = Field(min_length=1, max_length=50)
