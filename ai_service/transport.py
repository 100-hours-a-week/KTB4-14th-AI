"""Resolve explicit Korean day-scoped travel preferences without changing the API."""

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
    # BUS/SUBWAY are internal restrictions, never new public request enum values.
    return "PUBLIC_TRANSPORT" if mode in {"BUS", "SUBWAY"} else TRANSPORT_ALIASES[mode]


def resolve_day_transports(request: ItineraryRequest) -> dict[str, str]:
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
        # Do not extend a day's scope into a new unrelated sentence.
        clause = re.split(r"[.!?\n]", clause, maxsplit=1)[0]
        modes = list(MODE.finditer(clause))
        positive = set()
        for j, match in enumerate(modes):
            end = modes[j + 1].start() if j + 1 < len(modes) else len(clause)
            suffix = clause[match.end():end]
            # A shared predicate also negates coordinated names: "버스나 지하철은 필요 없다".
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
                raise GenerationFailed("추가 요청의 이동수단 날짜 범위가 여행 기간과 맞지 않습니다.")
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
            raise GenerationFailed("추가 요청의 이동수단 적용 날짜가 여행 기간을 벗어납니다.")
        overrides.setdefault(date, set()).update(positive)
    for date, modes in overrides.items():
        # A named bus/subway refines the broad public-transport choice.
        if modes & {"BUS", "SUBWAY"}:
            modes.discard("PUBLIC_TRANSPORT")
        if len(modes) != 1:
            raise GenerationFailed(f"{date}의 이동수단 요청이 여러 가지입니다. 날짜별로 하나를 지정해주세요.")
        result[date] = next(iter(modes))
    return result
