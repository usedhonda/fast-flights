from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional


@dataclass
class Result:
    current_price: Literal["low", "typical", "high"]
    flights: List[Flight]


@dataclass
class Flight:
    is_best: bool
    name: str
    departure: str
    arrival: str
    arrival_time_ahead: str
    duration: str
    stops: int | str
    delay: Optional[str]
    price: str
    origin_airport: str | None = None
    destination_airport: str | None = None
    return_departure: str | None = None
    return_arrival: str | None = None
    return_duration: str | None = None
    return_stops: int | str | None = None
    return_origin_airport: str | None = None
    return_destination_airport: str | None = None
    return_leg_available: bool = False
