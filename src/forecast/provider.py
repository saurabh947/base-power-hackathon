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
    ) -> ForecastBook:
        model, source, cache_dir = _resolve(model, source, cache_dir)
        if source == "db":
            df = store.read_db(model, series, location, start, end)
        else:
            df = store.read_parquet(cache_dir, model, series, location, start, end)
        return cls.from_frame(df, location, series, model)


def latest_forecast(
    location: str,
    at_utc: datetime | str,
    series: str = "rt_price",
    *,
    model: str | None = None,
    source: str | None = None,
    cache_dir: Path | None = None,
) -> dict | None:
    """The latest forecast with issued_at <= at_utc as a docs/API.md Forecast dict, or None."""
    at = pd.Timestamp(at_utc)
    if at.tzinfo is None:
        raise ValueError("at_utc must be timezone-aware (e.g. '2025-05-16T21:00:00Z')")
    model, source, cache_dir = _resolve(model, source, cache_dir)
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
