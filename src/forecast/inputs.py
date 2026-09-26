"""Model inputs with strict no look-ahead.

The rule (enforced here, tested in tests/test_forecast_inputs.py): a forecast
issued at T uses only intervals that ENDED at or before T. RT interval
[ts, ts + 15 min) is usable iff ts + 15 min <= T; hourly load likewise with
1 h. This matches the forecasts table ("only data strictly before issued_at")
and the decision lag: the price of an interval is published when it ends.

Day-ahead prices are the one known-future input. ERCOT posts DA results for
operating day D at about 13:30 CT on D-1 (forecast.da_publish_local). At T
they are known through the end of local day D_T, or D_T + 1 once 13:30 CT
has passed. A horizon step past that point takes the DA price 24 h earlier,
which is always known. Prices are real (src.data.series); nothing is invented.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NS_PER_MIN = 60 * 10**9


@dataclass(frozen=True)
class Grid:
    """A regular series in memory: values[i] belongs to interval [t0 + i*step, t0 + (i+1)*step).

    Missing intervals are NaN. Build it once per year and slice per issue time.
    """

    t0: pd.Timestamp  # UTC start of values[0]
    step_minutes: int
    values: np.ndarray  # float32

    @classmethod
    def from_series(cls, s: pd.Series, step_minutes: int) -> Grid:
        if s.empty:
            raise ValueError("empty series")
        idx = pd.DatetimeIndex(s.index).tz_convert("UTC")
        if idx.has_duplicates:
            raise ValueError("duplicate timestamps")
        step = pd.Timedelta(minutes=step_minutes)
        if ((idx - idx[0]) % step != pd.Timedelta(0)).any():
            raise ValueError(f"timestamps are not on a {step_minutes}-minute grid")
        full = pd.date_range(idx.min(), idx.max(), freq=step)
        values = pd.Series(np.asarray(s, dtype=np.float64), index=idx).reindex(full).to_numpy(np.float32)
        return cls(full[0], step_minutes, values)

    def ts(self, i: int) -> pd.Timestamp:
        return self.t0 + pd.Timedelta(minutes=self.step_minutes * i)

    def ended_before(self, issue: datetime) -> int:
        """Number of leading intervals that ENDED at or before `issue` (index of the first open one)."""
        delta = pd.Timestamp(issue).tz_convert("UTC") - self.t0
        ended = delta // pd.Timedelta(minutes=self.step_minutes)
        return int(min(max(ended, 0), len(self.values)))

    def context(self, issue: datetime, steps: int) -> np.ndarray:
        """The last `steps` values whose interval ended at or before `issue` (fewer near the start)."""
        end = self.ended_before(issue)
        return self.values[max(0, end - steps):end]

    def first_open(self, issue: datetime) -> pd.Timestamp:
        """Start of the first interval not yet ended at `issue`: where a forecast begins."""
        delta = pd.Timestamp(issue).tz_convert("UTC") - self.t0
        return self.ts(int(delta // pd.Timedelta(minutes=self.step_minutes)))


def issue_times(start: datetime, end: datetime, every_minutes: int) -> pd.DatetimeIndex:
    """Issue times in [start, end), on UTC multiples of `every_minutes`."""
    step = pd.Timedelta(minutes=every_minutes)
    s, e = pd.Timestamp(start).tz_convert("UTC"), pd.Timestamp(end).tz_convert("UTC")
    first = s.ceil(step) if every_minutes <= 60 * 24 else s
    return pd.date_range(first, e, freq=step, inclusive="left")


def da_known_until(issue: datetime, publish_local: time = time(13, 30), tz: str = "America/Chicago") -> pd.Timestamp:
    """UTC instant up to which DA prices are published at `issue`."""
    zone = ZoneInfo(tz)
    local = pd.Timestamp(issue).tz_convert(zone)
    days_ahead = 2 if local.time() >= publish_local else 1
    last_day = local.date() + timedelta(days=days_ahead)  # calendar days: DST days are 23 or 25 h long
    return pd.Timestamp(datetime.combine(last_day, time(0), tzinfo=zone)).tz_convert("UTC")


def da_on_grid(da_hourly: pd.Series, grid: Grid) -> np.ndarray:
    """Hourly DA prices mapped onto `grid` (each step takes the DA price of its hour). NaN if unknown."""
    idx = pd.date_range(grid.t0, periods=len(grid.values), freq=pd.Timedelta(minutes=grid.step_minutes))
    hourly = pd.Series(np.asarray(da_hourly, dtype=np.float64),
                       index=pd.DatetimeIndex(da_hourly.index).tz_convert("UTC"))
    return hourly.reindex(idx.floor("h")).to_numpy(np.float32)


def da_covariate(
    da: np.ndarray,
    grid: Grid,
    issue: datetime,
    context_steps: int,
    horizon_steps: int,
    publish_local: time = time(13, 30),
    tz: str = "America/Chicago",
) -> np.ndarray:
    """(1, context + horizon) DA prices aligned with grid.context(issue, context_steps) + the horizon.

    Past steps take the DA price of their hour. Future steps take it only if it
    was published by `issue`; otherwise the DA price 24 h earlier.
    """
    end = grid.ended_before(issue)
    start = max(0, end - context_steps)
    out = np.full(end - start + horizon_steps, np.nan, dtype=np.float32)
    lo, hi = start, min(end + horizon_steps, len(da))
    out[: hi - lo] = da[lo:hi]
    known_until = da_known_until(issue, publish_local, tz)
    per_day = 24 * 60 // grid.step_minutes
    for j in range(end - start, len(out)):
        i = start + j
        if grid.ts(i) >= known_until or not np.isfinite(out[j]):
            out[j] = da[i - per_day] if 0 <= i - per_day < len(da) else np.nan
    return out[None, :]
