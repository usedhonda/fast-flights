from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional


@dataclass
class Result:
    current_price: Literal["low", "typical", "high"]
    flights: List[Flight]


@dataclass
class Emissions:
    kg_co2e: int | None = None
    delta_percent: int | None = None
    relative_label: str | None = None


@dataclass
class Layover:
    airport_code: str | None = None
    duration_text: str | None = None
    duration_min: int | None = None


@dataclass
class FarePolicy:
    carry_on_included: bool | None = None
    checked_bag_included: bool | None = None
    changeable: bool | None = None
    refundable: bool | None = None


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
    self_transfer: bool = False
    emissions: Emissions = field(default_factory=Emissions)
    layovers: list[Layover] = field(default_factory=list)
    flight_numbers: list[str] = field(default_factory=list)
    operated_by: str | None = None
    aircraft: str | None = None
    amenities: list[str] = field(default_factory=list)
    fare_policy: FarePolicy = field(default_factory=FarePolicy)
