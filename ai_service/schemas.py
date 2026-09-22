from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
import unicodedata
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
    model_serializer,
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

    @field_validator("full_name", mode="before")
    @classmethod
    def normalize_full_name(cls, value):
        # Canonically equivalent Hangul must produce the same provider search.
        return unicodedata.normalize("NFC", value) if isinstance(value, str) else value


class Duration(StrictModel):
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


class Preference(StrictModel):
    pace_type: NonEmpty
    transport_type: NonEmpty
    budget_min: int | None = Field(default=None, ge=0)
    budget_max: int | None = Field(default=None, ge=0)
    budget_type: str = Field(
        default="KRW", pattern=r"^[A-Z]{3}$",
        description="스프레드시트 기준 화폐 단위. 요청·응답 모두 budget_type 사용",
    )
    distance_preference: int | None = Field(default=None, ge=0, le=100)
    themes: list[NonEmpty] = Field(default_factory=list, max_length=10)
    foods: list[NonEmpty] = Field(default_factory=list, max_length=10)
    extra_request: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_preferences(self):
        if self.pace_type not in PACE_ALIASES:
            raise ValueError("unsupported pace_type")
        if self.transport_type not in TRANSPORT_ALIASES:
            raise ValueError("unsupported transport_type")
        # Keep accepted input aliases, but emit the ERD's canonical enum values.
        self.pace_type = PACE_ALIASES[self.pace_type]
        self.transport_type = TRANSPORT_ALIASES[self.transport_type]
        if self.budget_min is not None and self.budget_max is not None:
            if self.budget_min > self.budget_max:
                raise ValueError("budget_max must be at least budget_min")
        return self


class RequiredPlace(StrictModel):
    provider: Literal["KAKAO"]
    provider_place_id: NonEmpty
    place_name: NonEmpty
    address: str = Field(default="", max_length=500)
    road_address: str = Field(default="", max_length=500)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    category: str = Field(default="", max_length=500)
    order: int = Field(ge=1)


class ItineraryRequest(StrictModel):
    """Internal generation context; HTTP requests use TravelGenerationRequest."""

    generation_job_id: int | None = Field(default=None, gt=0)
    travel_plan_id: int | None = Field(default=None, gt=0)
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


class LegacyGenerationPreference(Preference):
    budget_type: str = Field(default="KRW", pattern=r"^[A-Z]{3}$")


class LegacyGenerationRequest(StrictModel):
    """Nested user request, accepted alongside the backend's flat DTO."""

    generation_job_id: int = Field(gt=0)
    region: Region
    duration: Duration
    headcount: int = Field(ge=1, le=30)
    companion_type: NonEmpty
    preference: LegacyGenerationPreference
    required_places: list[RequiredPlace] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_required_places(self):
        ids = [p.provider_place_id for p in self.required_places]
        orders = [p.order for p in self.required_places]
        if len(set(ids)) != len(ids) or len(set(orders)) != len(orders):
            raise ValueError("required_places IDs and orders must be unique")
        return self

    def generation_context(self) -> ItineraryStreamRequest:
        # Preserve the request keys and job ID; never reinterpret it as a travel plan ID.
        return ItineraryStreamRequest.model_validate(self.model_dump(by_alias=False))


class TravelGenerationPreference(Preference):
    budget_type: str = Field(pattern=r"^[A-Z]{3}$")
    distance_preference: int = Field(ge=0, le=100)
    themes: list[NonEmpty] = Field(max_length=3)


class BackendPlaceContext(StrictModel):
    provider: Literal["KAKAO"]
    provider_place_id: NonEmpty
    place_name: NonEmpty | None = None
    address: str | None = Field(default=None, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    place_type: Literal["TOURISM", "RESTAURANT", "ACCOMMODATION"] = "TOURISM"
    order: int | None = Field(default=None, ge=1)


class TravelGenerationRequest(Duration):
    """Matches feature-travel's AiTravelGenerationRequest, not the frontend DTO."""

    travel_plan_id: int = Field(gt=0)
    region_id: int = Field(gt=0)
    region_name: NonEmpty
    headcount: int = Field(ge=1, le=30)
    companion_type: NonEmpty
    preference: TravelGenerationPreference
    required_places: list[BackendPlaceContext] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_required_places(self):
        ids = [p.provider_place_id for p in self.required_places]
        orders = [p.order if p.order is not None else i for i, p in enumerate(self.required_places, 1)]
        if len(set(ids)) != len(ids) or len(set(orders)) != len(orders):
            raise ValueError("required_places IDs and orders must be unique")
        return self

    def generation_context(self) -> ItineraryStreamRequest:
        from ai_service.errors import GenerationFailed

        places = []
        for index, place in enumerate(self.required_places, 1):
            if place.place_name is None or place.latitude is None or place.longitude is None:
                raise GenerationFailed("필수 장소의 장소명·위도·경도가 없습니다. 백엔드에서 재생성 장소 상세를 복구해 전달해 주세요.")
            places.append(RequiredPlace(
                provider=place.provider, provider_place_id=place.provider_place_id,
                place_name=place.place_name, address=place.address or "",
                latitude=place.latitude, longitude=place.longitude,
                category={"TOURISM": "관광", "RESTAURANT": "식당", "ACCOMMODATION": "숙소"}[place.place_type],
                order=place.order if place.order is not None else index,
            ))
        return ItineraryStreamRequest(
            travel_plan_id=self.travel_plan_id,
            region=Region(region_id=self.region_id, full_name=self.region_name),
            duration=Duration(arrival_datetime=self.arrival_datetime, departure_datetime=self.departure_datetime),
            headcount=self.headcount,
            companion_type=self.companion_type,
            preference=self.preference,
            required_places=places,
        )


class PlaceResponse(StrictModel):
    provider: Literal["KAKAO"] = "KAKAO"
    provider_place_id: NonEmpty
    place_name: NonEmpty
    address: NonEmpty
    road_address: str = ""
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    category: Category


class Place(PlaceResponse):
    # Provider metadata is kept internally for candidate selection only.
    source_category: str
    is_required: bool = False


# Model output contains only candidate IDs and scheduling decisions. It cannot invent
# coordinates, place names or provider identifiers in the public response.
class SelectionItem(StrictModel):
    provider_place_id: str


class SelectionDay(StrictModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    items: list[SelectionItem] = Field(max_length=10)


class ModelSelection(StrictModel):
    title: str = Field(min_length=1, max_length=50)
    days: list[SelectionDay] = Field(min_length=1, max_length=8)


class ModelItem(StrictModel):
    provider_place_id: str
    start_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    stay_minutes: int = Field(gt=0, le=1440)


class ModelDay(StrictModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    items: list[ModelItem] = Field(max_length=10)


class ModelItinerary(StrictModel):
    title: str = Field(min_length=1, max_length=50)
    days: list[ModelDay] = Field(min_length=1, max_length=8)


class Coordinate(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RouteStop(StrictModel):
    name: str
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    station_id: str | None = None
    # Displayed stop number from a verified stop data source; not a provider ID.
    station_number: str | None = Field(default=None, min_length=1, max_length=32)


class WalkingInstruction(StrictModel):
    description: str
    distance_meter: int = Field(ge=0)
    path: list[Coordinate] = Field(
        default_factory=list, description="이 도보 안내에 해당하는 경로 부분의 좌표"
    )


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
    stops: list[RouteStop] = Field(default_factory=list)
    path: list[Coordinate] = Field(
        default_factory=list,
        description="이 이동 구간을 지도에 선으로 그리는 순서 있는 좌표. 방문 장소 목록이 아님",
    )
    instructions: list[WalkingInstruction] = Field(default_factory=list)
    vehicles: list[RouteVehicle] = Field(default_factory=list)


class RouteDetails(StrictModel):
    transport_type: str
    duration_minutes: int = Field(ge=0)
    distance_meter: int = Field(ge=0)
    is_estimated: bool = Field(
        default=True,
        description="서버 좌표 기반 추정이면 true, 카카오 길찾기 결과이면 false. 실제 도착 시각 보증이 아님",
    )
    provider: Literal["KAKAO", "GEOGRAPHIC_ESTIMATE"] = "GEOGRAPHIC_ESTIMATE"
    map_url: str | None = None
    departure_datetime: datetime | None = None
    arrival_datetime: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    transit_available: bool | None = None
    walking_fallback: bool = False
    message: str | None = None
    legs: list[RouteLeg] = Field(default_factory=list)


class TransitStopSummary(StrictModel):
    name: NonEmpty
    station_number: str | None = Field(
        default=None, min_length=1, max_length=32,
        description="버스 정류장 표시 번호. 앞자리 0을 유지하는 문자열. 별도 정류장 데이터로 확인한 경우만 제공하며 현재 카카오 단독 조회는 null. 내부 station_id·장소 ID·버스 번호와 다름",
    )


class TransitLegSummary(StrictModel):
    mode: Literal["BUS", "SUBWAY", "TRAIN", "EXPRESSBUS", "AIRPLANE", "FERRY"]
    line_name: NonEmpty
    vehicle_number: str | None = None
    start: TransitStopSummary
    end: TransitStopSummary


class RouteSummary(StrictModel):
    """Public route contract; detailed geometry stays inside the route provider."""

    transport_type: str
    duration_minutes: int = Field(ge=0)
    distance_meter: int = Field(ge=0)
    line_name: str | None = Field(default=None, description="대중교통 탑승 구간이 하나일 때의 노선명. 환승은 legs 순서 참조")
    vehicle_number: str | None = Field(default=None, description="대중교통 탑승 구간이 하나일 때의 버스 번호")
    legs: list[TransitLegSummary] = Field(default_factory=list, description="탑승 순서의 노선·버스 번호·승하차 정류장/역. 상세 좌표와 전체 정류장 목록은 제외")

    @model_serializer(mode="wrap")
    def serialize_summary(self, handler):
        data = handler(self)
        for key in ("line_name", "vehicle_number"):
            if data[key] is None:
                data.pop(key)
        if not data["legs"]:
            data.pop("legs")
        return data


class ItineraryItem(PlaceResponse):
    sequence: int
    item_type: Literal["TOUR", "RESTAURANT", "ACCOMMODATION"]
    start_time: str
    end_time: str
    route_from_previous: RouteSummary | None = Field(
        default=None,
        description="이전 방문 항목에서 오는 이동수단·시간·거리·대중교통 탑승 안내. 여행 첫 장소는 null. 다음날 첫 항목은 전날 마지막 항목에서 출발. CAR는 좌표 기반 추정치",
    )


class ItineraryDay(StrictModel):
    day_number: int
    travel_date: date
    items: list[ItineraryItem]


class RequiredPlaceResponse(StrictModel):
    provider: str
    provider_place_id: str
    place_name: str
    address: str
    road_address: str
    latitude: float
    longitude: float
    category: str
    order: int


class ItineraryResponse(StrictModel):
    generation_job_id: int | None = None
    travel_plan_id: int | None = None
    region: Region
    duration: Duration
    headcount: int
    companion_type: str
    preference: Preference
    required_places: list[RequiredPlaceResponse]
    title: str = Field(min_length=1, max_length=50)
    days: list[ItineraryDay]

    @model_serializer(mode="wrap")
    def serialize_identity(self, handler):
        data = handler(self)
        for key in ("generation_job_id", "travel_plan_id"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class ErrorResponse(StrictModel):
    message: str
    data: dict | None


class MusicRecommendation(StrictModel):
    title: NonEmpty
    artist: NonEmpty
    youtube_url: HttpUrl = Field(description="검증된 곡명·가수의 YouTube 검색 링크. 직접 재생 URL이 아님")


class MusicCandidate(StrictModel):
    music_id: int = Field(gt=0)
    title: NonEmpty
    artist: NonEmpty
    youtube_url: HttpUrl


class MusicPreference(StrictModel):
    themes: list[NonEmpty] = Field(min_length=1, max_length=10)


class MusicRequest(StrictModel):
    travel_plan_id: int = Field(gt=0)
    region: Region
    duration: Duration
    preference: MusicPreference
    candidates: list[MusicCandidate] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_candidates(self):
        ids = [candidate.music_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidates music_id must be unique")
        return self


class MusicSelection(StrictModel):
    """Internal model selects an ID; metadata always comes from the request."""
    music_id: int


class SelectedMusic(MusicCandidate):
    travel_plan_id: int


class MusicResponse(StrictModel):
    message: Literal["ai_music_recommended"] = "ai_music_recommended"
    data: SelectedMusic


class ItineraryStreamRequest(ItineraryRequest):
    def itinerary_request(self) -> ItineraryRequest:
        return ItineraryRequest.model_validate(self.model_dump())


class MusicSuggestion(StrictModel):
    """Internal model output only: public metadata is verified by the catalogue."""
    title: NonEmpty
    artist: NonEmpty


class RecommendedItem(PlaceResponse):
    sequence: int


class RecommendedDay(StrictModel):
    day_number: int
    travel_date: date
    items: list[RecommendedItem]


class PlacesResult(StrictModel):
    title: str
    days: list[RecommendedDay]


class Accommodation(StrictModel):
    day_number: int
    travel_date: date
    place: PlaceResponse


class AccommodationsResult(StrictModel):
    accommodations: list[Accommodation]


class RoutesResult(StrictModel):
    itinerary: ItineraryResponse


class GenerationResult(RoutesResult):
    music: MusicRecommendation
