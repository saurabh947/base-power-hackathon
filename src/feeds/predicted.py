"""PriceFeed across the end of real prices: real ticks before a year's real_until_utc, the AI's prediction after.

    feed = PredictedFeed(real_feed, cutoff, {location: PredictionPath})

- Before `cutoff` every tick comes from the real feed (Postgres or the snapshot), untouched.
- From `cutoff` on, the price is the prediction's p50 (src/forecast/predictions.py, made by
  src/forecast/predict.py), forward-filled from its 15-min intervals to their own three 5-minute ticks like any
  real interval, rounded to cents. demand_mw and fuel_mix are None (nothing is predicted for them).
- A location without a prediction simply has no ticks after the cutoff, so a replay there ends at the cutoff.
- Real rows at or after the cutoff are never used, even if the real feed has some for a location (the cutoff is
  where EVERY fleet city's real prices end: src.data.years.real_until).

Consumers mark ticks at or after the cutoff as predicted (docs/API.md Tick.predicted); PriceTick itself is frozen
(src/core/models.py), so the flag lives with the caller, which knows the cutoff.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator, Mapping

import numpy as np

from src.core.models import PriceTick

UTC = timezone.utc


class PredictedFeed:
    def __init__(self, real, cutoff: datetime, paths: Mapping[str, object], tick_minutes: int = 5) -> None:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        self.real = real
        self.cutoff = cutoff.astimezone(UTC)
        self.paths = dict(paths)
        self.tick = timedelta(minutes=tick_minutes)
        self._tick_ns = tick_minutes * 60 * 10**9

    def ticks(self, start: datetime, end: datetime, location: str) -> Iterator[PriceTick]:
        for t in (start, end):
            if t.tzinfo is None or t.utcoffset() is None:
                raise ValueError(f"start and end must be timezone-aware, got {t!r}")
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if start >= end:
            return iter(())
        return self._generate(start, end, location)

    def _generate(self, start: datetime, end: datetime, location: str) -> Iterator[PriceTick]:
        if start < self.cutoff:
            yield from self.real.ticks(start, min(end, self.cutoff), location)
        path = self.paths.get(location)
        if end <= self.cutoff or path is None or not len(path.ts):
            return
        lo_ns = _ns(max(start, self.cutoff))
        hi_ns = _ns(end)
        step_ns = 15 * 60 * 10**9
        per = step_ns // self._tick_ns
        i0 = max(0, int(np.searchsorted(path.ts, lo_ns - step_ns, side="right")))
        for i in range(i0, len(path.ts)):
            t_ns = int(path.ts[i])
            if t_ns >= hi_ns:
                break
            if t_ns < _ns(self.cutoff):
                continue
            price = round(float(path.p50[i]), 2)
            for k in range(per):
                tk = t_ns + k * self._tick_ns
                if lo_ns <= tk < hi_ns:
                    yield PriceTick(datetime.fromtimestamp(tk // 10**9, UTC), location, price, None, None)


def _ns(t: datetime) -> int:
    return int(round(t.timestamp())) * 10**9
