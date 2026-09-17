from __future__ import annotations

import asyncio
import math
import re

import httpx
from pydantic import ValidationError

from ai_service.config import Settings
from ai_service.errors import GenerationFailed, ServiceUnavailable
from ai_service.schemas import ItineraryRequest, Place, TRANSPORT_ALIASES


THEME_QUERIES = {
    "NATURE": ("자연 관광", "AT4"),
    "FOOD": ("전통시장", ""),
    "CULTURE": ("박물관", "CT1"),
    "HISTORY": ("역사", "AT4"),
    "ACTIVITY": ("체험", ""),
    "HEALING": ("공원", "AT4"),
    "SHOPPING": ("쇼핑", ""),
    "PHOTO": ("전망대", "AT4"),
}
FOOD_QUERIES = {
    "KOREAN": "한식",
    "JAPANESE": "일식",
    "CHINESE": "중식",
    "WESTERN": "양식",
    "SEAFOOD": "해산물",
    "VEGETARIAN": "채식",
    "VEGAN": "비건",
    "ASIAN": "아시아음식",
}
GROUPS = {"AT4": "관광", "CT1": "관광", "FD6": "식당", "CE7": "식당", "AD5": "숙소"}


def category_of(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    if value in GROUPS:
        return GROUPS[value]
    if any(
        word in value.upper()
        for word in ("숙박", "숙소", "호텔", "펜션", "STAY", "ACCOMMODATION")
    ):
        return "숙소"
    if any(
        word in value.upper() for word in ("음식", "식당", "카페", "RESTAURANT", "FOOD")
    ):
        return "식당"
    if any(
        word in value.upper()
        for word in (
            "관광",
            "문화",
            "여행",
            "명소",
            "시장",
            "쇼핑",
            "체험",
            "레저",
            "TOUR",
            "ATTRACTION",
            "ACTIVITY",
        )
    ):
        return "관광"
    return None


def region_tokens(value: str) -> set[str]:
    aliases = {
        "제주특별자치도": "제주",
        "서울특별시": "서울",
        "부산광역시": "부산",
        "대구광역시": "대구",
        "인천광역시": "인천",
        "광주광역시": "광주",
        "대전광역시": "대전",
        "울산광역시": "울산",
        "세종특별자치시": "세종",
        "경기도": "경기",
        "강원특별자치도": "강원",
        "강원도": "강원",
        "전북특별자치도": "전북",
        "전라북도": "전북",
        "전라남도": "전남",
        "경상북도": "경북",
        "경상남도": "경남",
        "충청북도": "충북",
        "충청남도": "충남",
    }
    return {
        aliases.get(token, token) for token in re.split(r"\s+", value.strip()) if token
    }


def in_region(region: str, address: str) -> bool:
    if not isinstance(address, str):
        return False
    return region_tokens(region).issubset(region_tokens(address))


def distance_km(first: Place, second: Place) -> float:
    lat1, lat2 = math.radians(first.latitude), math.radians(second.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(second.longitude - first.longitude)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 6371 * 2 * math.asin(min(1.0, math.sqrt(a)))


def travel_minutes(first: Place, second: Place, transport: str) -> int:
    # Conservative geographic estimate, never advertised as a directions API result.
    mode = TRANSPORT_ALIASES[transport]
    speed, overhead = {"WALK": (4, 0), "PUBLIC_TRANSPORT": (18, 15), "CAR": (30, 10)}[
        mode
    ]
    if first.provider_place_id == second.provider_place_id:
        return 0
    return max(5, math.ceil(distance_km(first, second) * 1.4 / speed * 60 + overhead))


class KakaoPlaces:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings
        self.semaphore = asyncio.Semaphore(4)

    async def _get(self, endpoint: str, params: dict) -> list[dict]:
        if not self.settings.kakao_rest_api_key:
            raise ServiceUnavailable()
        try:
            async with self.semaphore:
                response = await self.client.get(
                    f"https://dapi.kakao.com/v2/local/search/{endpoint}.json",
                    params=params,
                    headers={
                        "Authorization": f"KakaoAK {self.settings.kakao_rest_api_key}"
                    },
                    timeout=self.settings.kakao_timeout_seconds,
                )
            response.raise_for_status()
            documents = response.json()["documents"]
            if not isinstance(documents, list) or not all(
                isinstance(d, dict) for d in documents
            ):
                raise ValueError("invalid documents")
            return documents
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ServiceUnavailable() from exc

    async def collect(
        self, request: ItineraryRequest, *, include_accommodation: bool = True
    ) -> list[Place]:
        pool: dict[str, Place] = {}
        for required in sorted(request.required_places, key=lambda p: p.order):
            category = category_of(required.category)
            if category is None:
                raise GenerationFailed(
                    "필수 장소의 카테고리를 관광·식당·숙소 중 하나로 확인해 주세요."
                )
            pool[required.provider_place_id] = Place(
                **required.model_dump(exclude={"order", "category"}),
                category=category,
                source_category=required.category,
                is_required=True,
            )

        documents = await self._get("address", {"query": request.region.full_name})
        if not documents:
            # Region search is only used to establish a geographic center.
            documents = await self._get(
                "keyword", {"query": request.region.full_name, "size": 1}
            )
        if not documents:
            raise GenerationFailed("여행 지역의 위치를 찾을 수 없습니다.")
        try:
            x, y = float(documents[0]["x"]), float(documents[0]["y"])
            if (
                not math.isfinite(x)
                or not math.isfinite(y)
                or not (-180 <= x <= 180 and -90 <= y <= 90)
            ):
                raise ValueError("invalid center")
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceUnavailable() from exc

        # Cluster new places around required stops (or the regional center).
        # A city-wide pool can otherwise produce several hours of zigzag transfers.
        mode = TRANSPORT_ALIASES[request.preference.transport_type]
        distance = (
            request.preference.distance_preference
            if request.preference.distance_preference is not None
            else 50
        )
        base, spread = {
            "WALK": (1500, 3500),
            "PUBLIC_TRANSPORT": (4000, 8000),
            "CAR": (6000, 14000),
        }[mode]
        radius = round(base + spread * distance / 100)
        anchors = [(p.longitude, p.latitude) for p in pool.values()][:3] or [(x, y)]
        base_queries = [("관광명소", "AT4", 3), ("음식점", "FD6", 2)]
        if include_accommodation:
            base_queries.append(("숙소", "AD5", 1))
        queries = []
        for theme in request.preference.themes[:3]:
            query, group = THEME_QUERIES.get(theme, (theme, ""))
            queries.append((query, group, 1))
        for food in request.preference.foods[:3]:
            queries.append((FOOD_QUERIES.get(food, food), "FD6", 1))
        # Preference-specific results take priority when the bounded pool is trimmed.
        queries += base_queries
        calls = []
        for query, group, pages in dict.fromkeys(queries):
            for anchor_x, anchor_y in anchors:
                for page in range(1, pages + 1):
                    params = {
                        "x": anchor_x,
                        "y": anchor_y,
                        "radius": radius,
                        "query": f"{request.region.full_name} {query}",
                        "size": 15,
                        "page": page,
                    }
                    if group:
                        params["category_group_code"] = group
                    calls.append(self._get("keyword", params))
        results = await asyncio.gather(*calls)
        limits = {"관광": 60, "식당": 45, "숙소": 15}
        counts = {category: 0 for category in limits}
        for result in results:
            for doc in result:
                try:
                    category = GROUPS.get(
                        doc.get("category_group_code", "")
                    ) or category_of(doc.get("category_name", ""))
                    if category is None or not in_region(
                        request.region.full_name, doc.get("address_name", "")
                    ):
                        continue
                    place = Place(
                        provider_place_id=doc["id"],
                        place_name=doc["place_name"],
                        address=doc["address_name"],
                        road_address=doc.get("road_address_name", ""),
                        latitude=float(doc["y"]),
                        longitude=float(doc["x"]),
                        category=category,
                        source_category=doc.get("category_name", category),
                    )
                except (ValidationError, KeyError, TypeError, ValueError):
                    continue
                if (
                    place.provider_place_id not in pool
                    and counts[category] < limits[category]
                ):
                    pool[place.provider_place_id] = place
                    counts[category] += 1
        if not any(not p.is_required for p in pool.values()):
            raise GenerationFailed(
                "해당 지역에서 새로 추천할 수 있는 카카오 장소가 없습니다."
            )
        return list(pool.values())

    async def accommodations(
        self, request: ItineraryRequest, latitude: float, longitude: float
    ) -> list[Place]:
        documents = await self._get(
            "category",
            {
                "category_group_code": "AD5",
                "x": longitude,
                "y": latitude,
                "radius": 20000,
                "sort": "distance",
                "size": 15,
            },
        )
        result = {}
        for doc in documents:
            try:
                if doc.get("category_group_code") != "AD5" or not in_region(
                    request.region.full_name, doc.get("address_name")
                ):
                    continue
                place = Place(
                    provider_place_id=doc["id"],
                    place_name=doc["place_name"],
                    address=doc["address_name"],
                    road_address=doc.get("road_address_name", ""),
                    latitude=float(doc["y"]),
                    longitude=float(doc["x"]),
                    category="숙소",
                    source_category=doc.get("category_name") or "숙박",
                )
            except (ValidationError, KeyError, TypeError, ValueError):
                continue
            result[place.provider_place_id] = place
        return list(result.values())
