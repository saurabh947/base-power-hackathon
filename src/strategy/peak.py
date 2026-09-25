"""Trivial alternative strategy (spec section 8): proves the seam swaps via config.

Charges during [10:00, 15:00) and discharges during [18:00, 21:00),
America/Chicago local time. The windows are half-open and follow DST through
zoneinfo. The strategy ignores prices and price history, and reads only tick.ts.
"""

from __future__ import annotations

import math
from datetime import time
from zoneinfo import ZoneInfo

from src.core.models import PriceTick


class PeakStrategy:
    def __init__(
        self,
        timezone: str = "America/Chicago",
        charge_window: tuple[time, time] = (time(10, 0), time(15, 0)),
        discharge_window: tuple[time, time] = (time(18, 0), time(21, 0)),
    ) -> None:
        for name, (start, end) in (("charge", charge_window), ("discharge", discharge_window)):
            if not start < end:
                raise ValueError(f"{name} window must start before it ends")
        if charge_window[0] < discharge_window[1] and discharge_window[0] < charge_window[1]:
            raise ValueError("charge and discharge windows overlap")
        self.timezone = timezone
        self._zone = ZoneInfo(timezone)
        self.charge_window = charge_window
        self.discharge_window = discharge_window

    def target_kw(self, tick: PriceTick,
                  fleet_soc: float,
                  fleet_headroom_kw: float,
                  price_history: list[float]) -> float:
        headroom = float(fleet_headroom_kw)
        if not (math.isfinite(headroom) and headroom > 0.0):
            return 0.0
        if tick.ts.tzinfo is None or tick.ts.utcoffset() is None:
            raise ValueError(f"tick.ts must be timezone-aware, got {tick.ts!r}")
        local = tick.ts.astimezone(self._zone).time()
        if self.charge_window[0] <= local < self.charge_window[1]:
            return -headroom
        if self.discharge_window[0] <= local < self.discharge_window[1]:
            return headroom
        return 0.0

    def __repr__(self) -> str:
        c, d = self.charge_window, self.discharge_window
        return (f"PeakStrategy(charge {c[0]:%H:%M}-{c[1]:%H:%M}, "
                f"discharge {d[0]:%H:%M}-{d[1]:%H:%M} {self.timezone})")
