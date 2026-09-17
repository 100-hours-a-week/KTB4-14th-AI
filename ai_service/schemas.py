from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


KST = ZoneInfo("Asia/Seoul")
NonEmpty = Annotated[str, Field(min_length=1, max_length=500)]
Category = Literal["관광", "식당", "숙소"]
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
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, allow_inf_nan=False
    )


class Region(StrictModel):
    region_id: int = Field(gt=0)
    full_name: NonEmpty


class Duration(StrictModel):
    arrival_datetime: datetime
    departure_datetime: datetime

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


class Preference(StrictModel):
    pace_type: NonEmpty
    transport_type: NonEmpty
    budget_min: int | None = Field(default=None, ge=0)
    budget_max: int | None = Field(default=None, ge=0)
    budget_currency: str = Field(default="KRW", pattern=r"^[A-Z]{3}$")
    distance_preference: int | None = Field(default=None, ge=0, le=100)
    themes: list[NonEmpty] = Field(min_length=1, max_length=10)
    foods: list[NonEmpty] = Field(default_factory=list, max_length=10)
    extra_request: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_preferences(self):
        if self.pace_type not in PACE_ALIASES:
            raise ValueError("unsupported pace_type")
        if self.transport_type not in TRANSPORT_ALIASES:
            raise ValueError("unsupported transport_type")
        if self.budget_min is not None and self.budget_max is not None:
            if self.budget_min > self.budget_max:
                raise ValueError("budget_max must be at least budget_min")
        return self


class RequiredPlace(StrictModel):
    provider: Literal["KAKAO"]
    provider_place_id: NonEmpty
    place_name: NonEmpty
    address: NonEmpty
    road_address: str = Field(default="", max_length=500)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    category: NonEmpty
    order: int = Field(ge=1)


class ItineraryRequest(StrictModel):
    generation_job_id: int = Field(gt=0)
    region: Region
    duration: Duration
    headcount: int = Field(ge=1, le=30)
    companion_type: NonEmpty
    preference: Preference
    required_places: list[RequiredPlace] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_required_places(self):
        ids = [p.provider_place_id for p in self.required_places]
        orders = [p.order for p in self.required_places]
        if len(set(ids)) != len(ids) or len(set(orders)) != len(orders):
            raise ValueError("required_places IDs and orders must be unique")
        return self


class PlaceResponse(StrictModel):
    provider: Literal["KAKAO"] = "KAKAO"
    provider_place_id: NonEmpty
    place_name: NonEmpty
    address: NonEmpty
    road_address: str = ""
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    category: Category
    is_required: bool = False


class Place(PlaceResponse):
    # Provider metadata is kept internally for candidate selection only.
    source_category: str


# Model output contains only candidate IDs and scheduling decisions. It cannot invent
# coordinates, place names or provider identifiers in the public response.
class SelectionItem(StrictModel):
    provider_place_id: str


class SelectionDay(StrictModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    items: list[SelectionItem] = Field(max_length=10)


class ModelSelection(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    days: list[SelectionDay] = Field(min_length=1, max_length=8)


class ModelItem(StrictModel):
    provider_place_id: str
    start_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    stay_minutes: int = Field(gt=0, le=360)


class ModelDay(StrictModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    items: list[ModelItem] = Field(max_length=10)


class ModelItinerary(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    days: list[ModelDay] = Field(min_length=1, max_length=8)


class Coordinate(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RouteStop(StrictModel):
    name: str
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    station_id: str | None = None


class WalkingInstruction(StrictModel):
    description: str
    distance_meter: int = Field(ge=0)
    path: list[Coordinate] = Field(default_factory=list)


class RouteVehicle(StrictModel):
    name: str
    type: str


class RouteLeg(StrictModel):
    mode: Literal[
        "WALK", "BUS", "SUBWAY", "EXPRESSBUS", "TRAIN", "AIRPLANE", "FERRY", "CAR"
    ]
    duration_seconds: int = Field(ge=0)
    distance_meter: int = Field(ge=0)
    start: RouteStop
    end: RouteStop
    departure_datetime: datetime
    arrival_datetime: datetime
    route_name: str | None = None
    route_id: str | None = None
    bus_number: str | None = None
    service_checked_at: datetime | None = None
    is_night_travel: bool = False
    stops: list[RouteStop] = Field(default_factory=list)
    path: list[Coordinate] = Field(default_factory=list)
    instructions: list[WalkingInstruction] = Field(default_factory=list)
    vehicles: list[RouteVehicle] = Field(default_factory=list)


class RouteDetails(StrictModel):
    transport_type: str
    duration_minutes: int = Field(ge=0)
    distance_meter: int = Field(ge=0)
    is_estimated: bool = True
    provider: Literal["KAKAO", "GEOGRAPHIC_ESTIMATE"] = "GEOGRAPHIC_ESTIMATE"
    schedule_verified: bool = False
    map_url: str | None = None
    departure_datetime: datetime | None = None
    arrival_datetime: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    transit_available: bool | None = None
    walking_fallback: bool = False
    message: str | None = None
    legs: list[RouteLeg] = Field(default_factory=list)


class ItineraryItem(PlaceResponse):
    sequence: int
    item_type: Literal["TOUR", "RESTAURANT", "ACCOMMODATION"]
    start_time: str
    end_time: str
    stay_minutes: int
    travel_minutes_from_previous: int
    route_from_previous: RouteDetails | None = None


class ItineraryDay(StrictModel):
    day_number: int
    date: date
    items: list[ItineraryItem]


class RequiredPlaceResponse(StrictModel):
    provider: str
    provider_place_id: str
    name: str
    address: str
    road_address: str
    latitude: float
    longitude: float
    category: str
    order: int


class ItineraryResponse(StrictModel):
    generation_job_id: int
    region: Region
    duration: Duration
    headcount: int
    companion_type: str
    preference: Preference
    required_places: list[RequiredPlaceResponse]
    title: str
    days: list[ItineraryDay]
    timezone: Literal["Asia/Seoul"] = "Asia/Seoul"
    model_version: str
    warnings: list[str]


class ErrorResponse(StrictModel):
    message: str
    data: dict | None


class MusicCandidate(StrictModel):
    music_id: int = Field(gt=0)
    title: NonEmpty
    artist: NonEmpty
    youtube_url: HttpUrl


class ItineraryStreamRequest(ItineraryRequest):
    music_candidates: list[MusicCandidate] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_music_candidates(self):
        ids = [candidate.music_id for candidate in self.music_candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("music_candidates IDs must be unique")
        return self

    def itinerary_request(self) -> ItineraryRequest:
        return ItineraryRequest.model_validate(
            self.model_dump(exclude={"music_candidates"})
        )


class MusicSelection(StrictModel):
    music_id: int


class RecommendedItem(PlaceResponse):
    sequence: int


class RecommendedDay(StrictModel):
    day_number: int
    date: date
    items: list[RecommendedItem]


class PlacesResult(StrictModel):
    title: str
    days: list[RecommendedDay]


class Accommodation(StrictModel):
    day_number: int
    date: date
    search_center: Coordinate
    place: PlaceResponse


class AccommodationsResult(StrictModel):
    accommodations: list[Accommodation]


class RouteSegment(RouteDetails):
    from_day_number: int
    to_day_number: int
    from_sequence: int
    to_sequence: int
    from_provider_place_id: str
    to_provider_place_id: str
    origin: Coordinate
    destination: Coordinate


class RoutesResult(StrictModel):
    itinerary: ItineraryResponse
    routes: list[RouteSegment]


class GenerationResult(RoutesResult):
    music: MusicCandidate
