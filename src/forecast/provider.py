"""Cached forecasts for the strategy, the backtest and the API.

    latest_forecast(location, at_utc, series="rt_price") -> Forecast dict | None
        One lookup: the latest forecast with issued_at <= at_utc, shaped like
        the Forecast object in docs/API.md. Used by GET /forecast and the live feed.
    ForecastBook.load(location, series="rt_price")
        Every forecast of one (model, series, location) in memory, with an O(log n)
        latest(at) lookup. Used per tick by ForecastStrategy and the backtest.

Source: Postgres when a database is configured (DATABASE_URL or DB_HOST),
otherwise the parquet cache (forecast.cache_dir). FORECAST_SOURCE=db|parquet
overrides. The model defaults to forecast.model in config.yaml. Nothing here
imports torch, so the API image does not need the forecast extra.

After a year's real_until_utc (src/data/years.py) there are no real prices to
forecast from, so rt_price forecasts there are VIRTUAL: one per hour, each the
next 24 h of the AI's stored prediction for the rest of the year
(src/forecast/predictions.py). ForecastBook.load drops any real forecast issued
after the cutoff (its context would be stale) and appends the virtual ones, and
latest_forecast answers from them. The strategy then sees its own long-range
prediction, which is also the price path it trades on after the cutoff.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from src.forecast import store


@dataclass(frozen=True)
class ForecastBook:
    location: str
    series: str
    model: str
    step_minutes: int
    issued: np.ndarray  # int64 ns since epoch (UTC), strictly increasing
    start: np.ndarray  # int64 ns: start of point 0 of each forecast
    p10: np.ndarray  # (n, horizon) float32
    p50: np.ndarray
    p90: np.ndarray

    def __len__(self) -> int:
        return len(self.issued)

    @property
    def horizon(self) -> int:
        return self.p50.shape[1] if len(self) else 0

    def latest(self, at: datetime | pd.Timestamp | int) -> int | None:
        """Index of the latest forecast issued at or before `at`, or None."""
        at_ns = at if isinstance(at, (int, np.integer)) else _ns(at)
        i = int(np.searchsorted(self.issued, at_ns, side="right")) - 1
        return i if i >= 0 else None

    def point_times(self, i: int) -> np.ndarray:
        """int64 ns start of each point of forecast i."""
        return self.start[i] + np.arange(self.horizon, dtype=np.int64) * self.step_minutes * 60 * 10**9

    def to_api(self, i: int) -> dict:
        """Forecast i as the docs/API.md Forecast object."""
        stamps = pd.to_datetime(self.point_times(i), utc=True)
        return {
            "location": self.location,
            "series": self.series,
            "model": self.model,
            "issued_utc": _iso(pd.Timestamp(self.issued[i], tz="UTC")),
            "step_minutes": self.step_minutes,
            "points": [
                {"ts_utc": _iso(ts), "p10": round(float(a), 2), "p50": round(float(b), 2), "p90": round(float(c), 2)}
                for ts, a, b, c in zip(stamps, self.p10[i], self.p50[i], self.p90[i])
            ],
        }

    @classmethod
    def from_frame(cls, df: pd.DataFrame, location: str, series: str, model: str) -> ForecastBook:
        """From store rows (any order). Rows must share step_minutes and horizon."""
        if df.empty:
            empty = np.zeros((0, 0), dtype=np.float32)
            return cls(location, series, model, 0, np.zeros(0, np.int64), np.zeros(0, np.int64), empty, empty, empty)
        df = df.sort_values("issued_at").drop_duplicates("issued_at", keep="last")
        steps = set(int(s) for s in df["step_minutes"])
        if len(steps) != 1:
            raise ValueError(f"mixed step_minutes {sorted(steps)} for {model}/{series}/{location}")

        def stack(col: str) -> np.ndarray:
            return np.stack([np.asarray(v, dtype=np.float32) for v in df[col]])

        return cls(
            location, series, model, steps.pop(),
            _ns_index(df["issued_at"]), _ns_index(df["start_ts"]),
            stack("p10"), stack("p50"), stack("p90"),
        )

    @classmethod
    def load(
        cls,
        location: str,
        series: str = "rt_price",
        *,
        model: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        source: str | None = None,
        cache_dir: Path | None = None,
        virtual: bool = False,
    ) -> ForecastBook:
        """Every stored forecast of (model, series, location) issued in [start, end). virtual=True (the forecast
        strategy asks for it; rt_price only): after each year's real_until_utc, the virtual forecasts sliced from the
        AI's prediction replace any real ones issued there."""
        model, source, cache_dir = _resolve(model, source, cache_dir)
        if source == "db":
            df = store.read_db(model, series, location, start, end)
        else:
            df = store.read_parquet(cache_dir, model, series, location, start, end)
        if virtual and series == "rt_price":
            df = with_virtual(df, location, model, source, start, end)
        return cls.from_frame(df, location, series, model)


def predicted_periods(raw=None) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """[(real_until_utc, year end)] of the configured years whose real prices end before the year does."""
    from src.data import years

    out = []
    for year in years.configured_years(raw):
        y = years.info(year)
        if y.available and y.real_until_utc is not None:
            out.append((pd.Timestamp(y.real_until_utc), pd.Timestamp(y.end_utc)))
    return out


def with_virtual(df: pd.DataFrame, location: str, model: str, source: str, start=None, end=None) -> pd.DataFrame:
    """Store rows with the virtual forecasts of every predicted period put in (see the module docstring)."""
    from src.forecast import predictions
    from src.forecast.settings import load_settings

    periods = predicted_periods()
    if not periods:
        return df
    cfg = load_settings().rt_price
    real, virtual = df, []
    for cutoff, year_end in periods:
        path = predictions.load(location, max_cutoff=cutoff.to_pydatetime(), model=model, source=source)
        if path is None:
            continue
        if not real.empty:  # a real forecast issued after the cutoff would have seen stale context
            issued = pd.to_datetime(real["issued_at"], utc=True)
            real = real[~((issued >= cutoff) & (issued < year_end))]
        lo = cutoff if start is None else max(cutoff, pd.Timestamp(start).tz_convert("UTC"))
        hi = year_end if end is None else min(year_end, pd.Timestamp(end).tz_convert("UTC"))
        if lo >= hi:
            continue
        v = predictions.virtual_forecasts(path, start=lo, end=hi, horizon_steps=cfg.horizon_steps,
                                          every_minutes=cfg.every_minutes)
        if not v.empty:
            virtual.append(v)
    parts = ([real] if not real.empty else []) + virtual
    if not parts:
        return real
    out = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
    return out.sort_values("issued_at").reset_index(drop=True)


def virtual_cutoff(at: datetime | pd.Timestamp) -> pd.Timestamp | None:
    """The real_until_utc after which `at` lies inside a predicted period (None: real prices at `at`)."""
    t = pd.Timestamp(at).tz_convert("UTC")
    for cutoff, year_end in predicted_periods():
        if cutoff <= t < year_end:
            return cutoff
    return None


def _virtual_latest(location: str, at: pd.Timestamp, model: str, source: str) -> dict | None:
    """The virtual forecast in force at `at` (issued at the last whole hour, not before the cutoff), or None."""
    from src.forecast import predictions
    from src.forecast.settings import load_settings

    cutoff = virtual_cutoff(at)
    if cutoff is None:
        return None
    issue = at.tz_convert("UTC").floor("60min")
    if issue < cutoff:
        return None  # before the first whole hour after the cutoff: the last real forecast is still the latest
    path = predictions.load(location, max_cutoff=cutoff.to_pydatetime(), model=model, source=source)
    if path is None:
        return None
    cfg = load_settings().rt_price
    v = predictions.virtual_forecasts(path, start=issue, end=issue + pd.Timedelta(minutes=cfg.every_minutes),
                                      horizon_steps=cfg.horizon_steps, every_minutes=cfg.every_minutes)
    if v.empty:
        return None
    return ForecastBook.from_frame(v, location, "rt_price", model).to_api(0)


def latest_forecast(
    location: str,
    at_utc: datetime | str,
    series: str = "rt_price",
    *,
    model: str | None = None,
    source: str | None = None,
    cache_dir: Path | None = None,
) -> dict | None:
    """The latest forecast with issued_at <= at_utc as a docs/API.md Forecast dict, or None. After a year's
    real_until_utc: the virtual forecast issued at the last whole hour (a slice of the AI's prediction)."""
    at = pd.Timestamp(at_utc)
    if at.tzinfo is None:
        raise ValueError("at_utc must be timezone-aware (e.g. '2025-05-16T21:00:00Z')")
    model, source, cache_dir = _resolve(model, source, cache_dir)
    if series == "rt_price":
        virtual = _virtual_latest(location, at, model, source)
        if virtual is not None:
            return virtual
    if source == "db":
        row = store.latest_row_db(model, series, location, at.to_pydatetime())
        if row is None:
            return None
        book = ForecastBook.from_frame(pd.DataFrame([row]), location, series, model)
        return book.to_api(0)
    book = _cached_book(str(cache_dir), model, series, location)
    i = book.latest(at)
    return None if i is None else book.to_api(i)


def clear_cache() -> None:
    """Forget parquet books read so far (after a job wrote new files)."""
    _cached_book.cache_clear()


@lru_cache(maxsize=32)
def _cached_book(cache_dir: str, model: str, series: str, location: str) -> ForecastBook:
    return ForecastBook.from_frame(store.read_parquet(Path(cache_dir), model, series, location), location, series, model)


def _resolve(model: str | None, source: str | None, cache_dir: Path | None) -> tuple[str, str, Path]:
    if model is None or cache_dir is None:
        from src.forecast.settings import load_settings

        settings = load_settings()
        model = model or settings.model
        cache_dir = cache_dir or settings.cache_dir
    source = source or store.default_source()
    if source not in ("db", "parquet"):
        raise ValueError("source must be db or parquet")
    return model, source, Path(cache_dir)


def _ns(ts: datetime | pd.Timestamp) -> int:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware, got {ts!r}")
    return int(t.tz_convert("UTC").value)


def _ns_index(values) -> np.ndarray:
    # pandas 3 infers microsecond resolution; pin ns so the int64s are ns everywhere.
    return pd.DatetimeIndex(pd.to_datetime(values, utc=True)).as_unit("ns").asi8.astype(np.int64)


def _iso(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
