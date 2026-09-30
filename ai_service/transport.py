"""추가 요청 문장에서 날짜별 이동수단 지정을 해석한다."""

from __future__ import annotations

from datetime import timedelta
import re

from ai_service.errors import GenerationFailed
from ai_service.schemas import ItineraryRequest, TRANSPORT_ALIASES


DAY = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})|"
    r"(?P<range_start>\d+)\s*(?:일\s*차)?\s*(?:[~～\-–]|부터)\s*"
    r"(?P<range_end>\d+)\s*일\s*차(?:\s*까지)?|"
    r"(?P<number>\d+)\s*일\s*차|"
    r"(?P<ordinal>첫째|둘째|셋째|넷째|다섯째|여섯째|일곱째|여덟째|첫|마지막)\s*날"
)
MODE = re.compile(
    r"자동차|렌터카|렌트카|자가용|자차|차량|택시|(?<![가-힣])차(?=로|를|는|\s|$)|"
    r"대중\s*교통|버스|지하철|도보|걸어서"
)
NEGATIVE = re.compile(
    r"상관\s*없|필요\s*없|무관|제외|생략|말고|말아|말았|않|아니|"
    r"(?:안|못)\s*(?:이용|타|탈|탑승|사용|추천|쓰)|없이"
)
ORDINALS = {
    name: n
    for n, name in enumerate(
        ("첫째", "둘째", "셋째", "넷째", "다섯째", "여섯째", "일곱째", "여덟째"), 1
    )
}
MODE_NAMES = {
    "대중교통": "PUBLIC_TRANSPORT", "버스": "BUS", "지하철": "SUBWAY",
    "도보": "WALK", "걸어서": "WALK",
}


def base_transport(mode: str) -> str:
    # BUS/SUBWAY는 내부 경로 제한이며 공개 요청의 새 enum 값은 아니다.
    return "PUBLIC_TRANSPORT" if mode in {"BUS", "SUBWAY"} else TRANSPORT_ALIASES[mode]


def resolve_day_transports(request: ItineraryRequest) -> dict[str, str]:
    """기본 이동수단에 날짜별 명시 요청을 적용하고 충돌을 검증한다."""
    arrival, departure = request.duration.local_bounds()
    dates = [
        (arrival.date() + timedelta(days=i)).isoformat()
        for i in range((departure.date() - arrival.date()).days + 1)
    ]
    result = dict.fromkeys(dates, base_transport(request.preference.transport_type))
    text = request.preference.extra_request or ""
    markers = list(DAY.finditer(text))
    overrides: dict[str, set[str]] = {}
    for i, marker in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        clause = text[marker.end():end]
        # 날짜 지정의 범위가 다음 문장까지 퍼지지 않게 한다.
        clause = re.split(r"[.!?\n]", clause, maxsplit=1)[0]
        modes = list(MODE.finditer(clause))
        positive = set()
        for j, match in enumerate(modes):
            end = modes[j + 1].start() if j + 1 < len(modes) else len(clause)
            suffix = clause[match.end():end]
            # "버스나 지하철은 필요 없다"처럼 함께 묶인 이동수단도 부정한다.
            k = j + 1
            while k < len(modes) and re.fullmatch(r"\s*(?:나|이나|와|과|또는|/|,|및)\s*", suffix):
                end = modes[k + 1].start() if k + 1 < len(modes) else len(clause)
                suffix = clause[modes[k].end():end]
                k += 1
            if NEGATIVE.search(suffix):
                continue
            word = re.sub(r"\s", "", match.group())
            mode = MODE_NAMES.get(word, "CAR")
            positive.add(mode)
        if not positive:
            continue
        if marker.group("range_start"):
            first, last = int(marker.group("range_start")), int(marker.group("range_end"))
            if not 1 <= first <= last <= len(dates):
                raise GenerationFailed("추가 요청의 이동수단 날짜 범위가 여행 기간과 맞지 않습니다.", reason="transport_day_range_invalid")
            for date in dates[first - 1:last]:
                overrides.setdefault(date, set()).update(positive)
            continue
        ordinal = marker.group("ordinal")
        if marker.group("number"):
            number = int(marker.group("number"))
        elif ordinal == "마지막":
            number = len(dates)
        else:
            number = 1 if ordinal == "첫" else ORDINALS.get(ordinal, 0)
        date = marker.group("date") or (dates[number - 1] if 1 <= number <= len(dates) else None)
        if date not in result:
            raise GenerationFailed("추가 요청의 이동수단 적용 날짜가 여행 기간을 벗어납니다.", reason="transport_day_out_of_range")
        overrides.setdefault(date, set()).update(positive)
    for date, modes in overrides.items():
        # 버스·지하철 지정이 있으면 포괄적인 대중교통 지정을 좁힌다.
        if modes & {"BUS", "SUBWAY"}:
            modes.discard("PUBLIC_TRANSPORT")
        if len(modes) != 1:
            raise GenerationFailed(f"{date}의 이동수단 요청이 여러 가지입니다. 날짜별로 하나를 지정해주세요.", reason="transport_day_conflict", detail={"date": date})
        result[date] = next(iter(modes))
    return result
