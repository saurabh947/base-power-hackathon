"""Forecast storage: the Postgres `forecasts` table (migrations/001_init.sql) or a parquet cache.

One row per issued forecast, the same shape in both stores:
    issued_at, location, series, model, start_ts, step_minutes, p10[], p50[], p90[]
Point i is at start_ts + i * step_minutes. The parquet cache lives under
forecast.cache_dir (data/cache/forecasts, gitignored), one file per job chunk:
    {cache_dir}/{model}/{series}/{location}/{first issue, UTC}.parquet
Writes are idempotent: the DB upserts on the primary key, and parquet readers
keep the last row per issued_at.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

COLUMNS = ["issued_at", "location", "series", "model", "start_ts", "step_minutes", "p10", "p50", "p90"]


@dataclass(frozen=True)
class ForecastRow:
    issued_at: pd.Timestamp  # UTC
    location: str
    series: str
    model: str
    start_ts: pd.Timestamp  # UTC start of point 0
    step_minutes: int
    p10: np.ndarray  # float32, one per step
    p50: np.ndarray
    p90: np.ndarray


def database_configured() -> bool:
    return bool(os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST"))


def default_source() -> str:
    """'db' when a database is configured, else 'parquet'. FORECAST_SOURCE overrides."""
    forced = os.environ.get("FORECAST_SOURCE")
    if forced:
        if forced not in ("db", "parquet"):
            raise ValueError("FORECAST_SOURCE must be db or parquet")
        return forced
    return "db" if database_configured() else "parquet"


# --- writing -------------------------------------------------------------------------


class ParquetSink:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)

    def write(self, rows: list[ForecastRow]) -> int:
        groups: dict[tuple[str, str, str], list[ForecastRow]] = {}
        for r in rows:
            groups.setdefault((r.model, r.series, r.location), []).append(r)
        for (model, series, location), part in groups.items():
            folder = self.cache_dir / model / series / location
            folder.mkdir(parents=True, exist_ok=True)
            first = min(r.issued_at for r in part).strftime("%Y%m%dT%H%M")
            _frame(part).to_parquet(folder / f"{first}.parquet", index=False)
        return len(rows)

    def existing(self, model: str, series: str, location: str, start: datetime, end: datetime) -> set[pd.Timestamp]:
        df = read_parquet(self.cache_dir, model, series, location, start, end, columns=["issued_at"])
        return set(df["issued_at"]) if not df.empty else set()


class DbSink:
    SQL = (
        "INSERT INTO forecasts (issued_at, location, series, model, start_ts, step_minutes, p10, p50, p90) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::real[], %s::real[], %s::real[]) "
        "ON CONFLICT (issued_at, location, series, model) DO UPDATE SET start_ts = EXCLUDED.start_ts, "
        "step_minutes = EXCLUDED.step_minutes, p10 = EXCLUDED.p10, p50 = EXCLUDED.p50, p90 = EXCLUDED.p90"
    )

    def __init__(self) -> None:
        from src import db

        self._conn = db.connect(autocommit=False)
        (table,) = self._conn.execute("SELECT to_regclass('forecasts')").fetchone()
        self._conn.commit()
        if table is None:
            self._conn.close()
            raise RuntimeError("the forecasts table is missing: run `python -m src.db migrate` (or the job's --migrate)")

    def write(self, rows: list[ForecastRow]) -> int:
        params = [
            (r.issued_at.to_pydatetime(), r.location, r.series, r.model, r.start_ts.to_pydatetime(), r.step_minutes,
             _floats(r.p10), _floats(r.p50), _floats(r.p90))
            for r in rows
        ]
        with self._conn.cursor() as cur:
            cur.executemany(self.SQL, params)
        self._conn.commit()
        return len(rows)

    def existing(self, model: str, series: str, location: str, start: datetime, end: datetime) -> set[pd.Timestamp]:
        cur = self._conn.execute(
            "SELECT issued_at FROM forecasts WHERE model = %s AND series = %s AND location = %s "
            "AND issued_at >= %s AND issued_at < %s",
            (model, series, location, start, end),
        )
        out = {pd.Timestamp(r[0]).tz_convert("UTC") for r in cur.fetchall()}
        self._conn.commit()
        return out

    def close(self) -> None:
        self._conn.close()


def _floats(a: np.ndarray) -> list[float]:
    return [float(x) for x in np.asarray(a, dtype=np.float32)]


def _frame(rows: Iterable[ForecastRow]) -> pd.DataFrame:
    rows = list(rows)
    return pd.DataFrame({
        "issued_at": pd.DatetimeIndex([r.issued_at for r in rows]).tz_convert("UTC"),
        "location": [r.location for r in rows],
        "series": [r.series for r in rows],
        "model": [r.model for r in rows],
        "start_ts": pd.DatetimeIndex([r.start_ts for r in rows]).tz_convert("UTC"),
        "step_minutes": np.array([r.step_minutes for r in rows], dtype=np.int32),
        "p10": [np.asarray(r.p10, np.float32) for r in rows],
        "p50": [np.asarray(r.p50, np.float32) for r in rows],
        "p90": [np.asarray(r.p90, np.float32) for r in rows],
    })


# --- reading ---------------------------------------------------------------------------


def read_parquet(
    cache_dir: Path,
    model: str,
    series: str,
    location: str,
    start: datetime | None = None,
    end: datetime | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """Rows with start <= issued_at < end, sorted, the last write per issued_at winning."""
    folder = Path(cache_dir) / model / series / location
    files = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
    if not files:
        return pd.DataFrame(columns=columns or COLUMNS)
    df = pd.concat([pd.read_parquet(f, columns=columns) for f in files], ignore_index=True)
    df["issued_at"] = pd.to_datetime(df["issued_at"], utc=True)
    df = _window(df, start, end)
    return df.drop_duplicates("issued_at", keep="last").sort_values("issued_at").reset_index(drop=True)


def read_db(
    model: str, series: str, location: str, start: datetime | None = None, end: datetime | None = None
) -> pd.DataFrame:
    from src import db

    clauses, params = ["model = %s", "series = %s", "location = %s"], [model, series, location]
    if start is not None:
        clauses.append("issued_at >= %s")
        params.append(start)
    if end is not None:
        clauses.append("issued_at < %s")
        params.append(end)
    sql = (f"SELECT {', '.join(COLUMNS)} FROM forecasts WHERE {' AND '.join(clauses)} ORDER BY issued_at")
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    df = pd.DataFrame(rows, columns=COLUMNS)
    if not df.empty:
        df["issued_at"] = pd.to_datetime(df["issued_at"], utc=True)
        df["start_ts"] = pd.to_datetime(df["start_ts"], utc=True)
    return df


def latest_row_db(model: str, series: str, location: str, at: datetime) -> dict | None:
    from src import db

    sql = (f"SELECT {', '.join(COLUMNS)} FROM forecasts WHERE model = %s AND series = %s AND location = %s "
           "AND issued_at <= %s ORDER BY issued_at DESC LIMIT 1")
    with db.connect() as conn:
        row = conn.execute(sql, (model, series, location, at)).fetchone()
    return dict(zip(COLUMNS, row)) if row else None


def _window(df: pd.DataFrame, start: datetime | None, end: datetime | None) -> pd.DataFrame:
    if start is not None:
        df = df[df["issued_at"] >= pd.Timestamp(start).tz_convert("UTC")]
    if end is not None:
        df = df[df["issued_at"] < pd.Timestamp(end).tz_convert("UTC")]
    return df
