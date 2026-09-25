"""Ledger for replays and backtests (spec section 9).

Prices grid-side delivered energy at the RT price of the tick it moved in:
usd = delivered_kwh * price_usd_mwh / 1000 (+ kWh = sold, - kWh = bought).
summary() is JSON-ready, for backtest_runs and the JSON export in M4.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Mapping
from zoneinfo import ZoneInfo

from src.core.models import PriceTick

SUMMARY_KEYS = ("total_usd", "per_location_usd", "per_battery_avg_usd", "sold_kwh", "bought_kwh", "top_spike_days")


class BacktestLedger:
    """In-memory Ledger. Takes the fleet size per location, for the per-battery average.

    Spike days are local dates in `timezone` (ERCOT runs on Central time).
    """

    def __init__(
        self,
        fleet_size_by_location: Mapping[str, int],
        *,
        timezone: str = "America/Chicago",
        top_n: int = 10,
    ) -> None:
        for location, size in fleet_size_by_location.items():
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError(f"fleet size for {location!r} must be an int >= 0, got {size!r}")
        self._fleet = dict(fleet_size_by_location)
        self._zone = ZoneInfo(timezone)
        self._top_n = top_n
        self._usd_by_location: dict[str, float] = {location: 0.0 for location in self._fleet}
        self._usd_by_day: dict[date, float] = {}
        self._sold_kwh = 0.0
        self._bought_kwh = 0.0

    def record(self, tick: PriceTick, delivered_kwh: float) -> None:
        """delivered_kwh: + = sold to grid, - = bought."""
        kwh = float(delivered_kwh)
        price = float(tick.price_usd_mwh)
        if not (math.isfinite(kwh) and math.isfinite(price)):
            raise ValueError(f"non-finite ledger entry: {kwh!r} kWh at {price!r} $/MWh ({tick.ts})")
        if tick.ts.tzinfo is None or tick.ts.utcoffset() is None:
            raise ValueError(f"tick.ts must be timezone-aware, got {tick.ts!r}")
        usd = kwh * price / 1000.0
        day = tick.ts.astimezone(self._zone).date()
        self._usd_by_location[tick.location] = self._usd_by_location.get(tick.location, 0.0) + usd
        self._usd_by_day[day] = self._usd_by_day.get(day, 0.0) + usd
        if kwh > 0:
            self._sold_kwh += kwh
        elif kwh < 0:
            self._bought_kwh -= kwh

    def summary(self) -> dict:
        total = math.fsum(self._usd_by_location.values())
        batteries = sum(self._fleet.values())
        best_days = sorted(self._usd_by_day.items(), key=lambda kv: (-kv[1], kv[0]))[: self._top_n]
        return {
            "total_usd": total,
            "per_location_usd": dict(self._usd_by_location),
            "per_battery_avg_usd": total / batteries if batteries else 0.0,
            "sold_kwh": self._sold_kwh,
            "bought_kwh": self._bought_kwh,
            "top_spike_days": [{"date": day.isoformat(), "usd": usd} for day, usd in best_days],
        }
