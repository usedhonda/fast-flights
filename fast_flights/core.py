import re
import urllib.parse
from typing import List, Literal, Optional

from selectolax.lexbor import LexborHTMLParser, LexborNode

from .schema import Emissions, Flight, Layover, Result
from .flights_impl import FlightData, Passengers
from .filter import TFSData
from .fallback_playwright import fallback_playwright_fetch
from .primp import Client, Response

_DIRECT_KEYWORDS = (
    "nonstop",
    "non-stop",
    "direct",
)

_STOP_PATTERNS = (
    re.compile(r"(?P<count>\d+)\s*stop(?:s)?", re.IGNORECASE),
)

_ROUTE_CODE_RE = re.compile(r"\b([A-Z]{3})\b.*?[–-].*?\b([A-Z]{3})\b")
_EN_LEG_RE = re.compile(
    r"Leaves .*? at (?P<dep>.+?) and arrives .*? at (?P<arr>.+?)\.",
    re.IGNORECASE,
)
_EMISSIONS_KG_RE = re.compile(r"(?P<kg>\d{2,4})\s*kg\s*CO2e", re.IGNORECASE)
_EMISSIONS_DELTA_SIGNED_RE = re.compile(
    r"(?P<delta>[+-]\d{1,3})%\s*emissions",
    re.IGNORECASE,
)
_EMISSIONS_DELTA_UNSIGN_RE = re.compile(
    r"(?P<delta>\d{1,3})%\s*(?P<label>less|more|lower|higher)",
    re.IGNORECASE,
)
_SELF_TRANSFER_RE = re.compile(
    r"self[- ]?transfer|separate(?:\s*&\s*self-transfer)?\s+tickets?",
    re.IGNORECASE,
)
_TRAVEL_IMPACT_FLIGHT_RE = re.compile(
    r"(?:^|,)[A-Z]{3}-[A-Z]{3}-(?P<carrier>[A-Z0-9]{2,3})-(?P<flight>\d{1,4})-\d{8}"
)
_LAYOVER_DURATION_AIRPORT_RE = re.compile(
    r"(?P<duration>\d+\s*hr(?:\s*\d+\s*min)?|\d+\s*min)\s+(?P<airport>[A-Z]{3})\b",
    re.IGNORECASE,
)
_LAYOVER_FROM_ARIA_RE = re.compile(
    r"(?P<duration>\d+\s*hr(?:\s*\d+\s*min)?|\d+\s*min)\s+layover",
    re.IGNORECASE,
)
_OPERATED_BY_RE = re.compile(
    r"operated by (?P<name>[A-Za-z0-9&.,'()\- ]{2,80}?)(?:\s+\d+\s*hr|\s+\d+\s*min|[.,]|$)",
    re.IGNORECASE,
)
_AIRCRAFT_RE = re.compile(r"\b(?:Boeing|Airbus)\s*[A-Z0-9-]{2,}\b", re.IGNORECASE)


def _duration_to_minutes(raw: str) -> int | None:
    text = _normalize_space(raw)
    if not text:
        return None

    lower = text.lower()
    hr_match = re.search(r"(\d+)\s*(?:hr|hour)", lower)
    min_match = re.search(r"(\d+)\s*(?:min|minute)", lower)
    if not hr_match and not min_match:
        return None

    hours = int(hr_match.group(1)) if hr_match else 0
    minutes = int(min_match.group(1)) if min_match else 0
    return hours * 60 + minutes


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

    return None, None


def _extract_emissions(route_text: str, aria_label: str) -> Emissions:
    merged = _normalize_space(f"{route_text} {aria_label}")
    kg_match = _EMISSIONS_KG_RE.search(merged)

    kg_co2e = int(kg_match.group("kg")) if kg_match else None
    delta_percent = None

    signed_match = _EMISSIONS_DELTA_SIGNED_RE.search(merged)
    if signed_match:
        delta_percent = int(signed_match.group("delta"))
    else:
        unsign_match = _EMISSIONS_DELTA_UNSIGN_RE.search(merged)
        if unsign_match:
            raw_delta = int(unsign_match.group("delta"))
            label = unsign_match.group("label").lower()
            delta_percent = -raw_delta if label in {"less", "lower"} else raw_delta

    relative_label = None
    merged_lower = merged.lower()
    if "avg emissions" in merged_lower or "average emissions" in merged_lower:
        relative_label = "average"
    elif delta_percent is not None:
        relative_label = "lower" if delta_percent < 0 else "higher"

    return Emissions(
        kg_co2e=kg_co2e,
        delta_percent=delta_percent,
        relative_label=relative_label,
    )


def _extract_flight_numbers(item: LexborNode) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    for node in item.css("*[data-travelimpactmodelwebsiteurl]"):
        url = node.attributes.get("data-travelimpactmodelwebsiteurl", "")
        if not url:
            continue

        parsed = urllib.parse.urlparse(url)
        itinerary_values = urllib.parse.parse_qs(parsed.query).get("itinerary", [])
        for itinerary in itinerary_values:
            decoded = urllib.parse.unquote(itinerary)
            for match in _TRAVEL_IMPACT_FLIGHT_RE.finditer(decoded):
                code = f"{match.group('carrier')}{match.group('flight')}"
                if code in seen:
                    continue
                seen.add(code)
                found.append(code)

    return found


def _extract_layovers(
    *, route_text: str, aria_label: str, stops: int | str
) -> list[Layover]:
    if not isinstance(stops, int) or stops <= 0:
        return []

    route = _normalize_space(route_text)
    aria = _normalize_space(aria_label)
    layovers: list[Layover] = []
    seen: set[tuple[str | None, str]] = set()

    stop_marker = re.search(
        r"\b\d+\s*stop(?:s)?\b",
        route,
        re.IGNORECASE,
    )
    if stop_marker:
        route_tail = route[stop_marker.end() :]
        for pattern in (_LAYOVER_DURATION_AIRPORT_RE,):
            for match in pattern.finditer(route_tail):
                duration_text = _normalize_space(match.group("duration"))
                airport_code = match.group("airport")
                key = (airport_code, duration_text)
                if key in seen:
                    continue
                seen.add(key)
                layovers.append(
                    Layover(
                        airport_code=airport_code,
                        duration_text=duration_text,
                        duration_min=_duration_to_minutes(duration_text),
                    )
                )

    for pattern in (_LAYOVER_FROM_ARIA_RE,):
        for match in pattern.finditer(aria):
            duration_text = _normalize_space(match.group("duration"))
            key = (None, duration_text)
            if key in seen:
                continue
            seen.add(key)
            layovers.append(
                Layover(
                    airport_code=None,
                    duration_text=duration_text,
                    duration_min=_duration_to_minutes(duration_text),
                )
            )

    return layovers


def _extract_operated_by(route_text: str, aria_label: str) -> str | None:
    def _normalize_operator(value: str | None) -> str | None:
        if value is None:
            return None

        cleaned = _normalize_space(value)
        if not cleaned:
            return None
        if len(cleaned) > 80:
            return None
        if re.search(r"\d{1,2}:\d{2}", cleaned):
            return None
        return cleaned

    merged = _normalize_space(f"{route_text} {aria_label}")
    match = _OPERATED_BY_RE.search(merged)
    if match:
        normalized = _normalize_operator(match.group("name"))
        if normalized:
            return normalized

    return None


def _extract_aircraft(route_text: str, aria_label: str) -> str | None:
    merged = _normalize_space(f"{route_text} {aria_label}")
    match = _AIRCRAFT_RE.search(merged)
    if not match:
        return None
    return _normalize_space(match.group(0))


def _extract_amenities(route_text: str) -> list[str]:
    normalized = _normalize_space(route_text).lower()
    amenities: list[str] = []

    if "wifi" in normalized or "wi-fi" in normalized:
        amenities.append("wifi")
    if "power outlet" in normalized or "usb" in normalized:
        amenities.append("power")
    if "legroom" in normalized:
        amenities.append("legroom")
    if "meal" in normalized or "snack" in normalized:
        amenities.append("meal")

    return amenities


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
            self_transfer = bool(
                _SELF_TRANSFER_RE.search(name)
                or _SELF_TRANSFER_RE.search(route_text)
                or _SELF_TRANSFER_RE.search(aria_label)
            )
            emissions = _extract_emissions(route_text, aria_label)
            layovers = _extract_layovers(
                route_text=route_text,
                aria_label=aria_label,
                stops=stops_fmt,
            )
            flight_numbers = _extract_flight_numbers(item)
            operated_by = _extract_operated_by(route_text, aria_label)
            aircraft = _extract_aircraft(route_text, aria_label)
            amenities = _extract_amenities(route_text)

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
                    "self_transfer": self_transfer,
                    "emissions": emissions,
                    "layovers": layovers,
                    "flight_numbers": flight_numbers,
                    "operated_by": operated_by,
                    "aircraft": aircraft,
                    "amenities": amenities,
                }
            )

    current_price = safe(parser.css_first("span.gOatQ")).text()
    if not flights:
        raise RuntimeError("No flights found:\n{}".format(r.text_markdown))

    return Result(current_price=current_price, flights=[Flight(**fl) for fl in flights])  # type: ignore
