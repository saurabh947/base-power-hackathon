"""The replay years visitors pick (docs/API.md Settings `year`, GET /settings `years`), and where each one's
real prices end.

- config.yaml `years:` lists the years (2025, 2026; no 2024) and the default one.
- real_until(year): the END of the last RT interval present for ALL the fleet's cities in that ERCOT
  (America/Chicago) calendar year. None when the whole year is real. The current year is published
  year-to-date (ERCOT republishes its annual files weekly), so its real prices stop there, and the AI's
  prediction (src/forecast/predict.py, table price_predictions) takes over after it.
- The source is the live fleet's (env PRICE_SOURCE, as src.sim.coordinator.price_feed): `postgres` reads the
  prices table, `snapshot` (the default) reads data/snapshot/ercot_rt_<year>_hubs.parquet.
- Answers are cached per process for CACHE_S (GET /settings asks on every call); refresh=True re-reads.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

UTC = timezone.utc
CHICAGO = ZoneInfo("America/Chicago")
INTERVAL = timedelta(minutes=15)
CACHE_S = 60.0
SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "data" / "snapshot"


@dataclass(frozen=True)
class YearInfo:
    """What real data a replay year has, for every fleet city at once."""

    year: int
    start_utc: datetime  # Jan 1 00:00 Central
    end_utc: datetime  # the next Jan 1 00:00 Central
    first_utc: datetime | None  # start of the first interval all cities have (None: no data)
    last_end_utc: datetime | None  # end of the last interval all cities have

    @property
    def available(self) -> bool:
        return self.first_utc is not None

    @property
    def real_until_utc(self) -> datetime | None:
        """docs/API.md Year.real_until_utc: the end of the last real interval, None when the whole year is real."""
        if self.last_end_utc is None or self.last_end_utc >= self.end_utc:
            return None
        return self.last_end_utc


def year_window(year: int) -> tuple[datetime, datetime]:
    """UTC [start, end) of an ERCOT calendar year: America/Chicago midnight Jan 1 to midnight Jan 1."""
    a = datetime(year, 1, 1, tzinfo=CHICAGO).astimezone(UTC)
    b = datetime(year + 1, 1, 1, tzinfo=CHICAGO).astimezone(UTC)
    return a, b


def local_year(ts: datetime) -> int:
    """The ERCOT (Central-time) year an instant belongs to."""
    return ts.astimezone(CHICAGO).year


# --- config -------------------------------------------------------------------------------------


def _section(raw: Mapping | None) -> dict:
    if raw is None:
        from src.config import load_raw_config

        raw = load_raw_config()
    return dict(raw.get("years") or {})


def configured_years(raw: Mapping | None = None) -> list[int]:
    """The years visitors can pick (config years.items), in config order."""
    items = _section(raw).get("items") or [2025]
    return [int(y) for y in items]


def default_year(raw: Mapping | None = None) -> int:
    section = _section(raw)
    return int(section.get("default") or configured_years(raw)[0])


def fleet_locations() -> tuple[str, ...]:
    from src.config import load_config

    return tuple(load_config().fleet.locations)


# --- where real prices end --------------------------------------------------------------------------


def price_source() -> str:
    """Where real prices come from: the live fleet's PRICE_SOURCE (snapshot by default, postgres in the cloud)."""
    source = os.environ.get("PRICE_SOURCE", "snapshot")
    if source not in ("snapshot", "postgres"):
        raise ValueError(f"PRICE_SOURCE must be snapshot or postgres, got {source!r}")
    return source


_cache: dict[tuple, tuple[float, YearInfo]] = {}
_lock = threading.Lock()


def info(year: int, *, locations: Sequence[str] | None = None, source: str | None = None,
         refresh: bool = False, snapshot_dir: Path | None = None) -> YearInfo:
    """Real-data coverage of `year` for every city in `locations` (default: the fleet's), cached CACHE_S."""
    locations = tuple(locations or fleet_locations())
    source = source or price_source()
    snap = Path(snapshot_dir) if snapshot_dir is not None else SNAPSHOT_DIR
    key = (int(year), locations, source, str(snap))
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and hit[0] > now:
            return hit[1]
    if source == "postgres":
        first, last = _common_range_db(int(year), locations)
    else:
        first, last = _common_range_snapshot(int(year), locations, snap)
    a, b = year_window(int(year))
    result = YearInfo(int(year), a, b, first, None if last is None else last + INTERVAL)
    with _lock:
        _cache[key] = (now + CACHE_S, result)
    return result


def real_until(year: int, **kwargs) -> datetime | None:
    """The end of the last RT interval present for all the fleet's cities in `year`; None when the year is
    complete (or has no data at all: see info().available)."""
    return info(year, **kwargs).real_until_utc


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def real_until_ahead(start: datetime, raw: Mapping | None = None, **kwargs) -> datetime | None:
    """The real_until_utc a replay starting at `start` runs into: that of the first configured year that has one
    and hasn't ended by `start` (None when every year from there on is real all year). A run that starts after
    it (a replay in the predicted months) is predicted from its first tick."""
    for year in sorted(configured_years(raw)):
        y = info(year, **kwargs)
        if y.available and y.real_until_utc is not None and y.end_utc > start:
            return y.real_until_utc
    return None


def _common_range_db(year: int, locations: tuple[str, ...]) -> tuple[datetime | None, datetime | None]:
    """(first, last) interval START present for every location, from the prices table."""
    from src.db import connect

    a, b = year_window(year)
    sql = ("SELECT min(ts), max(ts) FROM (SELECT ts FROM prices WHERE market = 'RT' AND location = ANY(%s) "
           "AND ts >= %s AND ts < %s GROUP BY ts HAVING count(DISTINCT location) = %s) t")
    with connect() as conn:
        first, last = conn.execute(sql, (list(locations), a, b, len(locations))).fetchone()
    return (None if first is None else first.astimezone(UTC), None if last is None else last.astimezone(UTC))


def _common_range_snapshot(year: int, locations: tuple[str, ...], snap: Path) -> tuple[datetime | None, datetime | None]:
    """(first, last) interval START present for every location, from the snapshot files (a year's first hours
    sit in the previous year's file when they are before 06:00 UTC, so both files are read)."""
    import pandas as pd

    a, b = year_window(year)
    frames = []
    for y in (year - 1, year):
        path = snap / f"ercot_rt_{y}_hubs.parquet"
        if path.exists():
            frames.append(pd.read_parquet(path, columns=["ts", "location"]))
    if not frames:
        return None, None
    df = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(df["ts"], utc=True)
    df = df[(ts >= pd.Timestamp(a)) & (ts < pd.Timestamp(b)) & df["location"].isin(list(locations))]
    counts = df.groupby("ts")["location"].nunique()
    common = counts.index[counts.to_numpy() == len(locations)]
    if len(common) == 0:
        return None, None
    return (pd.Timestamp(common.min()).tz_convert("UTC").to_pydatetime(),
            pd.Timestamp(common.max()).tz_convert("UTC").to_pydatetime())


# --- wording (GET /settings) ---------------------------------------------------------------------------


def last_real_day(real_until_utc: datetime) -> date:
    """The last local day with real prices (a real_until at local midnight ends the day before)."""
    return (real_until_utc - timedelta(microseconds=1)).astimezone(CHICAGO).date()


def short_date(d: date) -> str:
    """'Sep 20'."""
    return f"{d:%b} {d.day}"


def evening_before(real_until_utc: datetime, hour: int = 18) -> datetime:
    """6 pm Central on the evening before real prices end (at least 3 hours before them), as UTC: the start of the
    'Where real prices end' preset, so at 2 ticks/s the switch to predictions comes within a minute or two."""
    local = (real_until_utc - timedelta(hours=3)).astimezone(CHICAGO)
    day = local.date() if local.hour >= hour else local.date() - timedelta(days=1)
    return datetime.combine(day, dtime(hour), tzinfo=CHICAGO).astimezone(UTC)


def describe(y: YearInfo, *, predicted: bool) -> str:
    """The Year description: 'Real ERCOT prices for the whole year.' or 'Real prices through Sep 20, then the AI's
    prediction.' (predicted=False: its prediction hasn't been made yet)."""
    until = y.real_until_utc
    if until is None:
        return "Real ERCOT prices for the whole year."
    through = f"Real prices through {short_date(last_real_day(until))}"
    if predicted:
        return f"{through}, then the AI's prediction."
    return f"{through}. The AI's prediction for the rest of the year isn't ready yet."


def public_years(raw: Mapping | None = None, *, predicted: Mapping[int, bool] | None = None) -> list[dict]:
    """GET /settings `years`: [{year, label, description, real_until_utc}] for the configured years that have
    real prices for every city. predicted: {year: whether its prediction exists} (default: looked up)."""
    out = []
    for year in configured_years(raw):
        y = info(year)
        if not y.available:
            continue
        until = y.real_until_utc
        has_prediction = True
        if until is not None:
            has_prediction = (predicted or {}).get(year) if predicted is not None else prediction_ready(until)
        out.append({"year": year, "label": str(year), "description": describe(y, predicted=bool(has_prediction)),
                    "real_until_utc": None if until is None else until.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return out


def prediction_ready(cutoff: datetime) -> bool:
    """Whether every fleet city has a stored prediction that starts at or before `cutoff` (see predictions)."""
    try:
        from src.forecast import predictions
    except ImportError:
        return False
    try:
        return predictions.ready(fleet_locations(), cutoff)
    except Exception:
        return False
