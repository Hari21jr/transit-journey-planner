"""Itinerary types — what a solved query actually hands back."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..gtfs.loader import Feed, format_time


@dataclass
class Leg:
    """One continuous movement: either riding a vehicle or walking."""

    kind: str  # "ride" | "walk"
    from_stop: str
    to_stop: str
    depart: int
    arrive: int

    # Ride legs only
    route_label: str = ""
    headsign: str = ""
    trip_id: str = ""
    intermediate_stops: int = 0

    @property
    def duration(self) -> int:
        return self.arrive - self.depart


@dataclass
class Journey:
    """A complete origin-to-destination itinerary."""

    legs: list[Leg] = field(default_factory=list)

    @property
    def depart(self) -> int:
        return self.legs[0].depart if self.legs else -1

    @property
    def arrive(self) -> int:
        return self.legs[-1].arrive if self.legs else -1

    @property
    def duration(self) -> int:
        return self.arrive - self.depart

    @property
    def transfers(self) -> int:
        """Vehicle boardings after the first. Walking is not a transfer."""
        return max(0, sum(1 for leg in self.legs if leg.kind == "ride") - 1)

    @property
    def walking_seconds(self) -> int:
        return sum(leg.duration for leg in self.legs if leg.kind == "walk")

    def describe(self, feed: Feed) -> list[str]:
        """Human-readable itinerary lines."""
        def name(stop_id: str) -> str:
            stop = feed.stops.get(stop_id)
            return stop.name if stop else stop_id

        out: list[str] = []
        for leg in self.legs:
            if leg.kind == "walk":
                out.append(
                    f"{format_time(leg.depart)}  walk {leg.duration // 60} min "
                    f"to {name(leg.to_stop)}"
                )
            else:
                stops = (f" ({leg.intermediate_stops} stops)"
                         if leg.intermediate_stops else "")
                out.append(
                    f"{format_time(leg.depart)}  route {leg.route_label}"
                    f"{' to ' + leg.headsign if leg.headsign else ''}"
                    f" from {name(leg.from_stop)}{stops}"
                )
                out.append(f"{format_time(leg.arrive)}  arrive {name(leg.to_stop)}")
        return out

    def summary(self) -> str:
        return (f"{format_time(self.depart)} → {format_time(self.arrive)}  "
                f"({self.duration // 60} min, {self.transfers} transfer"
                f"{'' if self.transfers == 1 else 's'}, "
                f"{self.walking_seconds // 60} min walking)")
