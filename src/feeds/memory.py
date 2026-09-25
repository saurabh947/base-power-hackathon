"""In-memory PriceFeed, plus the 15-min to 5-min forward fill.

Used by the M0 skeleton and by the tests. The Postgres replay feed (M1) must
behave the same way. Both run tests/test_contracts_pricefeed.py.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

from src.core.models import PriceTick

UTC = timezone.utc


def forward_fill(rows: Iterable[PriceTick], interval_minutes: int = 15, tick_minutes: int = 5) -> list[PriceTick]:
    """Expand native interval rows (ts = interval start) into tick rows.

    Each interval's price is held for its own ticks only (15 -> 3 x 5 min).
    A missing interval stays missing. Nothing is filled across a gap.
    """
    if interval_minutes <= 0 or tick_minutes <= 0 or interval_minutes % tick_minutes:
        raise ValueError("interval_minutes must be a positive multiple of tick_minutes")
    per_interval = interval_minutes // tick_minutes
    step = timedelta(minutes=tick_minutes)
    out = []
    for row in rows:
        _require_aware(row.ts, "interval ts")
        if row.ts.microsecond or int(row.ts.timestamp()) % (interval_minutes * 60):
            raise ValueError(f"{row.ts.isoformat()} is not on the {interval_minutes}-minute grid")
        for k in range(per_interval):
            out.append(PriceTick(row.ts + k * step, row.location, row.price_usd_mwh, row.demand_mw, row.fuel_mix))
    return out


class InMemoryPriceFeed:
    """PriceFeed over ticks held in memory (any input order; stored sorted, as UTC)."""

    def __init__(self, ticks: Iterable[PriceTick], tick_seconds: int = 300) -> None:
        by_location: dict[str, list[PriceTick]] = {}
        for t in ticks:
            _require_aware(t.ts, "tick ts")
            if t.ts.microsecond or int(t.ts.timestamp()) % tick_seconds:
                raise ValueError(f"{t.ts.isoformat()} is not on the {tick_seconds}-second grid")
            if not math.isfinite(t.price_usd_mwh):
                raise ValueError(f"non-finite price at {t.ts.isoformat()} {t.location}")
            if t.ts.tzinfo is not UTC:
                t = PriceTick(t.ts.astimezone(UTC), t.location, t.price_usd_mwh, t.demand_mw, t.fuel_mix)
            by_location.setdefault(t.location, []).append(t)
        self._ticks: dict[str, list[PriceTick]] = {}
        self._ts: dict[str, list[datetime]] = {}
        for location, rows in by_location.items():
            rows.sort(key=lambda r: r.ts)
            stamps = [r.ts for r in rows]
            for a, b in zip(stamps, stamps[1:]):
                if a == b:
                    raise ValueError(f"duplicate tick {a.isoformat()} for {location}")
            self._ticks[location] = rows
            self._ts[location] = stamps

    @classmethod
    def from_intervals(
        cls, rows: Iterable[PriceTick], interval_minutes: int = 15, tick_minutes: int = 5
    ) -> InMemoryPriceFeed:
        """Build from native interval rows, as stored in the prices table."""
        return cls(forward_fill(rows, interval_minutes, tick_minutes), tick_seconds=tick_minutes * 60)

    def ticks(self, start: datetime, end: datetime, location: str) -> Iterator[PriceTick]:
        _require_aware(start, "start")
        _require_aware(end, "end")
        rows = self._ticks.get(location)
        if not rows:
            return iter(())
        stamps = self._ts[location]
        lo = bisect_left(stamps, start)
        hi = bisect_left(stamps, end)
        return iter(rows[lo:hi])


def _require_aware(ts: datetime, what: str) -> None:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware, got {ts!r}")
