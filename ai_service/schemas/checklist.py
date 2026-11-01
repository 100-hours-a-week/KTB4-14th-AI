"""여행 체크리스트 생성 요청과 검증된 생성 결과."""
from datetime import date, datetime
import re
from typing import Annotated, Literal
import unicodedata

from pydantic import BeforeValidator, Field, ValidationInfo, field_validator, model_validator

from ai_service.schemas.common import KST, NonEmpty, StrictModel


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


def calendar_datetime(value):
    if not isinstance(value, datetime) and not (isinstance(value, str) and 'T' in value):
        raise ValueError('ISO datetime with a date and time is required')
    return value


CalendarDateTime = Annotated[datetime, BeforeValidator(calendar_datetime)]
ClockTime = Annotated[str, Field(pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')]


class ChecklistPreference(StrictModel):
    themes: list[Theme] = Field(min_length=1, max_length=3)

    @field_validator('themes')
    @classmethod
    def validate_themes(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('themes must be unique')
        return values


class ChecklistItineraryItem(StrictModel):
    day_number: PositiveInt
    date: CalendarDate
    place_name: NonEmpty
    item_type: NonEmpty
    start_time: ClockTime
    end_time: ClockTime


class ChecklistRequest(StrictModel):
    """AI 서비스 API 시트 D26의 평면 여행·일정 DTO."""
    travel_plan_id: PositiveInt
    region_name: NonEmpty
    arrival_datetime: CalendarDateTime
    departure_datetime: CalendarDateTime
    headcount: PositiveInt
    companion_type: NonEmpty
    preference: ChecklistPreference
    itinerary_items: list[ChecklistItineraryItem] = Field(max_length=500)

    @field_validator('departure_datetime')
    @classmethod
    def validate_departure(cls, value: datetime, info: ValidationInfo):
        start = info.data.get('arrival_datetime')
        if start is not None:
            if bool(start.tzinfo) != bool(value.tzinfo):
                raise ValueError('both datetimes must use the same timezone convention')
            if value < start:
                raise ValueError('departure_datetime must be on or after arrival_datetime')
        return value

    def local_dates(self) -> tuple[date, date]:
        def local(value):
            return value.astimezone(KST).date() if value.tzinfo else value.date()
        return local(self.arrival_datetime), local(self.departure_datetime)

    @model_validator(mode='after')
    def validate_itinerary_dates(self):
        first, last = self.local_dates()
        for item in self.itinerary_items:
            if not first <= item.date <= last:
                raise ValueError('itinerary date must be within the trip dates in Asia/Seoul')
            if item.day_number != (item.date - first).days + 1:
                raise ValueError('itinerary day_number must match its trip date')
        return self


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
