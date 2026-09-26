from __future__ import annotations

import asyncio
import math
import re
import unicodedata
import time

import httpx
from pydantic import ValidationError

from ai_service.config import Settings
from ai_service.diagnostics import record
from ai_service.errors import GenerationFailed, ServiceUnavailable
from ai_service.schemas import ItineraryRequest, Place, TRANSPORT_ALIASES
from ai_service.transport import base_transport, resolve_day_transports


THEME_QUERIES = {
    "NATURE": ("자연 관광", "AT4"),
    "FOOD": ("전통시장", ""),
    "CULTURE": ("박물관", "CT1"),
    "HISTORY": ("역사", "AT4"),
    "ACTIVITY": ("체험", ""),
    "HEALING": ("공원", "AT4"),
    "REST": ("공원", "AT4"),
    "SHOPPING": ("쇼핑", ""),
    "PHOTO": ("전망대", "AT4"),
    "SNS": ("전망대", "AT4"),
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
        aliases.get(token, token)
        for token in re.split(r"\s+", unicodedata.normalize("NFC", value).strip())
        if token
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
        self._canonical_regions: dict[str, str] = {}

    async def _get(self, endpoint: str, params: dict) -> list[dict]:
        started = time.monotonic()
        if not self.settings.kakao_rest_api_key:
            raise ServiceUnavailable(reason="kakao_api_key_missing")
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
            record("place_provider_failed", provider="kakao", endpoint=endpoint,
                   http_status=exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
                   error_type=type(exc).__name__, elapsed_ms=round((time.monotonic() - started) * 1000))
            raise ServiceUnavailable(reason="kakao_places_request_failed", detail={"endpoint": endpoint, "error": type(exc).__name__, "http_status": exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None}) from exc

    async def collect(
        self, request: ItineraryRequest, *, include_accommodation: bool = True
    ) -> list[Place]:
        pool: dict[str, Place] = {}
        for required in sorted(request.required_places, key=lambda p: p.order):
            if not required.address or not required.category:
                documents = await self._get("keyword", {
                    "query": required.place_name,
                    "x": required.longitude, "y": required.latitude,
                    "radius": 2000, "size": 15,
                })
                document = next((doc for doc in documents if str(doc.get("id")) == required.provider_place_id), None)
                if document is None:
                    raise GenerationFailed("필수 장소의 주소·카테고리를 확인할 수 없습니다.", reason="required_place_not_found", detail={"provider_place_id": required.provider_place_id})
                required.address = required.address or document.get("address_name") or ""
                required.road_address = required.road_address or document.get("road_address_name") or ""
                required.category = required.category or document.get("category_group_code") or document.get("category_name") or ""
                if not required.address or category_of(required.category) is None:
                    raise GenerationFailed("필수 장소의 주소·카테고리를 확인할 수 없습니다.", reason="required_place_not_found", detail={"provider_place_id": required.provider_place_id})
            category = category_of(required.category)
            if category is None:
                raise GenerationFailed(
                    "필수 장소의 카테고리를 관광·식당·숙소 중 하나로 확인해 주세요.",
                    reason="required_place_category_unsupported",
                    detail={"provider_place_id": required.provider_place_id},
                )
            pool[required.provider_place_id] = Place(
                **required.model_dump(exclude={"order", "category"}),
                category=category,
                source_category=required.category,
                is_required=True,
            )

        documents = await self._get("address", {"query": request.region.full_name})
        # Resolve aliases using the provider's administrative address, never by
        # stripping city/district suffixes (which can silently broaden the area).
        regions = {d.get("address_name"): d for d in documents
                   if d.get("address_type") == "REGION" and d.get("address_name")}
        if len(regions) > 1:
            raise GenerationFailed("여행 지역을 시·군·구까지 명확하게 지정해 주세요.", reason="ambiguous_region")
        region = request.region.full_name
        if regions:
            region, document = next(iter(regions.items()))
            documents = [document]
        if len(self._canonical_regions) >= 128:
            self._canonical_regions.pop(next(iter(self._canonical_regions)))
        self._canonical_regions[request.region.full_name] = region
        if not documents:
            # Region search is only used to establish a geographic center.
            documents = await self._get(
                "keyword", {"query": request.region.full_name, "size": 1}
            )
        if not documents:
            raise GenerationFailed("여행 지역의 위치를 찾을 수 없습니다.", reason="region_not_found")
        try:
            x, y = float(documents[0]["x"]), float(documents[0]["y"])
            if (
                not math.isfinite(x)
                or not math.isfinite(y)
                or not (-180 <= x <= 180 and -90 <= y <= 90)
            ):
                raise ValueError("invalid center")
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceUnavailable(reason="region_center_invalid") from exc

        # Cluster new places around required stops (or the regional center).
        # A city-wide pool can otherwise produce several hours of zigzag transfers.
        modes = {base_transport(m) for m in resolve_day_transports(request).values()}
        mode = min(modes, key={"WALK": 0, "PUBLIC_TRANSPORT": 1, "CAR": 2}.get)
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
        for theme in (request.preference.themes or ["NATURE"])[:3]:
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
                        "query": f"{region} {query}",
                        "size": 15,
                        "page": page,
                    }
                    if group:
                        params["category_group_code"] = group
                    calls.append(self._get("keyword", params))
        results = await asyncio.gather(*calls)
        limits = {"관광": 60, "식당": 45, "숙소": 15}
        counts = {category: 0 for category in limits}
        stats = {"received": 0, "outside_region": 0, "unsupported_category": 0, "invalid_document": 0}

        def add_results(batches):
            for result in batches:
                for doc in result:
                    stats["received"] += 1
                    try:
                        category = GROUPS.get(
                            doc.get("category_group_code", "")
                        ) or category_of(doc.get("category_name", ""))
                        if category is None:
                            stats["unsupported_category"] += 1
                            continue
                        if not in_region(region, doc.get("address_name", "")):
                            stats["outside_region"] += 1
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
                        stats["invalid_document"] += 1
                        continue
                    if (
                        place.provider_place_id not in pool
                        and counts[category] < limits[category]
                    ):
                        pool[place.provider_place_id] = place
                        counts[category] += 1
        add_results(results)
        # Keyword search may be empty even when category search has real places.
        # One bounded fallback, same radius/anchors and the same region checks.
        needed = [("관광", "AT4"), ("식당", "FD6")]
        if include_accommodation:
            needed.append(("숙소", "AD5"))
        missing = [group for category, group in needed if not any(p.category == category for p in pool.values())]
        if missing:
            fallback = await asyncio.gather(*(self._get("category", {
                "category_group_code": group, "x": ax, "y": ay, "radius": radius,
                "sort": "distance", "size": 15,
            }) for group in missing for ax, ay in anchors))
            add_results(fallback)
        record("place_collection", canonical_region=region, radius=radius, anchors=len(anchors),
               fallback_groups=missing, accepted=counts, required_count=len(request.required_places), **stats)
        if not any(not p.is_required for p in pool.values()):
            raise GenerationFailed(
                "해당 지역에서 새로 추천할 수 있는 카카오 장소가 없습니다.", reason="no_place_candidates"
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
        region = self._canonical_regions.get(request.region.full_name, request.region.full_name)
        result, outside = {}, {}
        for doc in documents:
            try:
                if doc.get("category_group_code") != "AD5":
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
            target = result if in_region(region, place.address) else outside
            target[place.provider_place_id] = place
        if not result and outside:
            # Anchors near a region border (or a required place outside it) only have
            # lodging across the border; a nearby stay beats failing the whole trip.
            record("accommodation_region_fallback", canonical_region=region, candidates=len(outside))
            return list(outside.values())
        return list(result.values())
