"""Time series for forecasting, backtests and the API.

Every function returns pandas objects indexed by UTC interval START, sliced to
[start, end). The backend is Postgres when a database is configured
(DATABASE_URL or DB_HOST) and SERIES_BACKEND != "snapshot"; otherwise it is
the committed parquet snapshot in data/snapshot. Callers never care which.
The Postgres backend is added in M1 (src/ingest owner) with identical output.
"""
from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd

SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "data" / "snapshot"


def backend() -> str:
    if os.environ.get("SERIES_BACKEND") == "snapshot":
        return "snapshot"
    return "postgres" if (os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST")) else "snapshot"


def rt_prices(location: str, start: datetime, end: datetime) -> pd.Series:
    """Real-time settlement point prices, native 15-min, $/MWh."""
    if backend() == "postgres":
        return _pg().rt_prices(location, start, end)
    return _prices("rt", location, start, end)


def da_prices(location: str, start: datetime, end: datetime) -> pd.Series:
    """Day-ahead settlement point prices, hourly, $/MWh."""
    if backend() == "postgres":
        return _pg().da_prices(location, start, end)
    return _prices("da", location, start, end)


def system_load(start: datetime, end: datetime) -> pd.Series:
    """ERCOT-wide native load, hourly, MW."""
    if backend() == "postgres":
        return _pg().system_load(start, end)
    df = _read("ercot_hourly_native_load_2025")
    s = pd.Series(df["ERCOT"].to_numpy(), index=pd.DatetimeIndex(df["Interval Start"]).tz_convert("UTC"), name="ERCOT")
    return _slice(s, start, end)


def fuel_mix(start: datetime, end: datetime) -> pd.DataFrame:
    """Generation shares by fuel, 15-min. Fractions of positive generation;
    WSL (storage charging, negative) is excluded."""
    if backend() == "postgres":
        return _pg().fuel_mix(start, end)
    df = _read("ercot_fuel_mix_15min_2025").drop(columns=["WSL"], errors="ignore").clip(lower=0)
    shares = df.div(df.sum(axis=1), axis=0)
    shares.columns = [str(c).lower() for c in shares.columns]
    return _slice(shares, start, end)


def _prices(market: str, location: str, start: datetime, end: datetime) -> pd.Series:
    parts = []
    # Files hold ERCOT (Central-time) years, which end at 06:00 UTC on Jan 1, so the
    # previous year's file can hold the first hours of a UTC year.
    for year in range(pd.Timestamp(start).year - 1, pd.Timestamp(end).year + 1):
        path = SNAPSHOT_DIR / f"ercot_{market}_{year}_hubs.parquet"
        if path.exists():
            df = _read(path.stem)
            df = df[df["location"] == location]
            parts.append(pd.Series(df["price_usd_mwh"].to_numpy(), index=pd.DatetimeIndex(df["ts"]), name=location))
    if not parts:
        return pd.Series(dtype="float64", name=location, index=pd.DatetimeIndex([], tz="UTC"))
    return _slice(pd.concat(parts).sort_index(), start, end)


@lru_cache(maxsize=16)
def _read(stem: str) -> pd.DataFrame:
    return pd.read_parquet(SNAPSHOT_DIR / f"{stem}.parquet")


def _slice(obj, start: datetime, end: datetime):
    start_ts, end_ts = pd.Timestamp(start).tz_convert("UTC"), pd.Timestamp(end).tz_convert("UTC")
    return obj[(obj.index >= start_ts) & (obj.index < end_ts)]


def _pg():
    try:
        from src.data import series_pg
    except ImportError as exc:  # lands in M1
        raise NotImplementedError("Postgres series backend lands in M1 (src/data/series_pg.py)") from exc
    return series_pg
