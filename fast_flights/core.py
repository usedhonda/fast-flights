import re
from typing import List, Literal, Optional

from selectolax.lexbor import LexborHTMLParser, LexborNode

from .schema import Flight, Result
from .flights_impl import FlightData, Passengers
from .filter import TFSData
from .fallback_playwright import fallback_playwright_fetch
from .primp import Client, Response

_DIRECT_KEYWORDS = (
    "nonstop",
    "non-stop",
    "direct",
    "直行便",
    "直行",
)

_STOP_PATTERNS = (
    re.compile(r"(?P<count>\d+)\s*stop(?:s)?", re.IGNORECASE),
    re.compile(r"(?P<count>\d+)\s*か所経由"),
    re.compile(r"(?P<count>\d+)\s*回乗り継ぎ"),
    re.compile(r"(?P<count>\d+)\s*回経由"),
)

_ROUTE_CODE_RE = re.compile(r"\b([A-Z]{3})\b.*?[–-].*?\b([A-Z]{3})\b")
_EN_LEG_RE = re.compile(
    r"Leaves .*? at (?P<dep>.+?) and arrives .*? at (?P<arr>.+?)\.",
    re.IGNORECASE,
)
_JA_LEG_RE = re.compile(
    r"[、 ](?P<dep>\d{1,2}:\d{2}).*?発、.*?[、 ](?P<arr>\d{1,2}:\d{2}).*?着"
)


def _normalize_space(value: str) -> str:
    return " ".join(value.replace("\u202f", " ").replace("\xa0", " ").split())


def _parse_stops(raw: str) -> int | str:
    value = _normalize_space(raw).lower()
    if not value:
        return "Unknown"

    if any(keyword in value for keyword in _DIRECT_KEYWORDS):
        return 0

    for pattern in _STOP_PATTERNS:
        match = pattern.search(value)
        if match:
            return int(match.group("count"))

    return "Unknown"


def _extract_route_codes(raw: str) -> tuple[str | None, str | None]:
    text = _normalize_space(raw)
    match = _ROUTE_CODE_RE.search(text)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def _extract_return_leg_times(raw_label: str) -> tuple[str | None, str | None]:
    label = _normalize_space(raw_label)
    if not label:
        return None, None

    english_legs = _EN_LEG_RE.findall(label)
    if len(english_legs) >= 2:
        dep, arr = english_legs[1]
        return _normalize_space(dep), _normalize_space(arr)

    japanese_legs = _JA_LEG_RE.findall(label)
    if len(japanese_legs) >= 2:
        dep, arr = japanese_legs[1]
        return _normalize_space(dep), _normalize_space(arr)

    return None, None


def fetch(params: dict) -> Response:
    client = Client(impersonate="chrome_126", verify=False)
    res = client.get("https://www.google.com/travel/flights", params=params)
    assert res.status_code == 200, f"{res.status_code} Result: {res.text_markdown}"
    return res


def get_flights_from_filter(
    filter: TFSData,
    currency: str = "",
    *,
    mode: Literal["common", "fallback", "force-fallback", "local"] = "common",
) -> Result:
    data = filter.as_b64()

    params = {
        "tfs": data.decode("utf-8"),
        "hl": "en",
        "tfu": "EgQIABABIgA",
        "curr": currency,
    }

    if mode in {"common", "fallback"}:
        try:
            res = fetch(params)
        except AssertionError as e:
            if mode == "fallback":
                res = fallback_playwright_fetch(params)
            else:
                raise e

    elif mode == "local":
        from .local_playwright import local_playwright_fetch

        res = local_playwright_fetch(params)

    else:
        res = fallback_playwright_fetch(params)

    try:
        return parse_response(res)
    except RuntimeError as e:
        if mode == "fallback":
            return get_flights_from_filter(filter, mode="force-fallback")
        raise e


def get_flights(
    *,
    flight_data: List[FlightData],
    trip: Literal["round-trip", "one-way", "multi-city"],
    passengers: Passengers,
    seat: Literal["economy", "premium-economy", "business", "first"],
    fetch_mode: Literal["common", "fallback", "force-fallback", "local"] = "common",
    max_stops: Optional[int] = None,
) -> Result:
    return get_flights_from_filter(
        TFSData.from_interface(
            flight_data=flight_data,
            trip=trip,
            passengers=passengers,
            seat=seat,
            max_stops=max_stops,
        ),
        mode=fetch_mode,
    )


def parse_response(
    r: Response, *, dangerously_allow_looping_last_item: bool = False
) -> Result:
    class _blank:
        def text(self, *_, **__):
            return ""

        def iter(self):
            return []

    blank = _blank()

    def safe(n: Optional[LexborNode]):
        return n or blank

    parser = LexborHTMLParser(r.text)
    flights = []

    for i, fl in enumerate(parser.css('div[jsname="IWWDBc"], div[jsname="YdtKid"]')):
        is_best_flight = i == 0

        for item in fl.css("ul.Rk10dc li")[
            : (None if dangerously_allow_looping_last_item or i == 0 else -1)
        ]:
            # Flight name
            name = safe(item.css_first("div.sSHqwe.tPgKwe.ogfYpf span")).text(
                strip=True
            )

            # Get departure & arrival time
            dp_ar_node = item.css("span.mv1WYe div")
            try:
                departure_time = dp_ar_node[0].text(strip=True)
                arrival_time = dp_ar_node[1].text(strip=True)
            except IndexError:
                # sometimes this is not present
                departure_time = ""
                arrival_time = ""

            # Get arrival time ahead
            time_ahead = safe(item.css_first("span.bOzv6")).text()

            # Get duration
            duration = safe(item.css_first("li div.Ak5kof div")).text()

            # Get flight stops
            stops = safe(item.css_first(".BbR8Ec .ogfYpf")).text()

            # Get delay
            delay = safe(item.css_first(".GsCCve")).text() or None

            # Get prices
            price = safe(item.css_first(".YMlIz.FpEdX")).text() or "0"

            stops_fmt = _parse_stops(stops)
            route_text = item.text(separator=" ", strip=True)
            origin_airport, destination_airport = _extract_route_codes(route_text)

            label_node = item.css_first('div[role="link"][aria-label]')
            aria_label = (
                label_node.attributes.get("aria-label", "")
                if label_node is not None
                else ""
            )
            return_departure, return_arrival = _extract_return_leg_times(aria_label)
            return_leg_available = bool(return_departure and return_arrival)

            flights.append(
                {
                    "is_best": is_best_flight,
                    "name": name,
                    "departure": " ".join(departure_time.split()),
                    "arrival": " ".join(arrival_time.split()),
                    "arrival_time_ahead": time_ahead,
                    "duration": duration,
                    "stops": stops_fmt,
                    "delay": delay,
                    "price": price.replace(",", ""),
                    "origin_airport": origin_airport,
                    "destination_airport": destination_airport,
                    "return_departure": return_departure,
                    "return_arrival": return_arrival,
                    "return_duration": None,
                    "return_stops": None,
                    "return_origin_airport": destination_airport if return_leg_available else None,
                    "return_destination_airport": origin_airport if return_leg_available else None,
                    "return_leg_available": return_leg_available,
                }
            )

    current_price = safe(parser.css_first("span.gOatQ")).text()
    if not flights:
        raise RuntimeError("No flights found:\n{}".format(r.text_markdown))

    return Result(current_price=current_price, flights=[Flight(**fl) for fl in flights])  # type: ignore
