"""The AI's prediction for the rest of a year: storage, lookup, and the "virtual" forecasts sliced from it.

A prediction is one path per (model, location, cutoff): 15-min RT prices from `cutoff` (the year's
real_until_utc: the model saw only real intervals that ended by then) to the end of the ERCOT year, with
p10/p50/p90. src/forecast/predict.py makes it. Two places read it:

- the replay (src/feeds/predicted.py): after real_until_utc a run's prices are the p50 path;
- the AI forecast strategy and GET /forecast (src/forecast/provider.py): after real_until_utc they get
  "virtual" forecasts, one per hour, each the next 24 h of the same path. The strategy then has a forecast to
  act on without looking at anything real, but it is its own long-range prediction, and the prices it trades
  on ARE that prediction's p50: after the cutoff its "forecast" is exact by construction, so its projected
  earnings there flatter it (docs/notes/forecast.md).

Storage: the Postgres table price_predictions (migrations/030_forecast_predictions.sql), or parquet under
forecast.predict.cache_dir (data/cache/predictions/{model}/{location}/{cutoff}.parquet). The source follows
the forecasts' (src.forecast.store.default_source: db when a database is configured, FORECAST_SOURCE overrides).
Writes are idempotent: the same cutoff replaces its path. Reads take the latest cutoff at or before the
real_until being replayed. No torch here.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from src.forecast import store

UTC = timezone.utc
STEP_MINUTES = 15
STEP_NS = STEP_MINUTES * 60 * 10**9
COLUMNS = ["model", "location", "cutoff", "ts", "issued_at", "chunk", "p10", "p50", "p90"]
CACHE_S = 60.0


@dataclass(frozen=True, eq=False)
class PredictionPath:
    """One location's predicted path: point i is the interval [ts[i], ts[i] + 15 min)."""

    location: str
    model: str
    cutoff: pd.Timestamp  # UTC; ts[0] == cutoff
    issued_at: pd.Timestamp  # UTC wall clock of the job
    ts: np.ndarray  # int64 ns interval starts, strictly increasing on the 15-min grid
    p10: np.ndarray  # float64 $/MWh
    p50: np.ndarray
    p90: np.ndarray
    chunk: np.ndarray  # int16: the chained step that made each point (0 saw only real prices)

    def __len__(self) -> int:
        return len(self.ts)

    @property
    def end(self) -> pd.Timestamp:
        """End of the last predicted interval."""
        return pd.Timestamp(int(self.ts[-1]) + STEP_NS, tz="UTC") if len(self) else self.cutoff

    def index_at(self, t_ns: int) -> int | None:
        """Index of the interval containing t_ns, or None outside the path or in a hole."""
        i = int(np.searchsorted(self.ts, t_ns, side="right")) - 1
        if i < 0 or t_ns >= int(self.ts[i]) + STEP_NS:
            return None
        return i

    def frame(self) -> pd.DataFrame:
        n = len(self)
        return pd.DataFrame({
            "model": [self.model] * n, "location": [self.location] * n,
            "cutoff": pd.DatetimeIndex([self.cutoff] * n).tz_convert("UTC"),
            "ts": pd.to_datetime(self.ts, utc=True), "issued_at": pd.DatetimeIndex([self.issued_at] * n).tz_convert("UTC"),
            "chunk": self.chunk.astype(np.int16), "p10": self.p10, "p50": self.p50, "p90": self.p90,
        })

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> PredictionPath:
        df = df.sort_values("ts")
        ts = pd.DatetimeIndex(pd.to_datetime(df["ts"], utc=True)).as_unit("ns").asi8.astype(np.int64)
        if len(ts) and (np.diff(ts) <= 0).any():
            raise ValueError("prediction timestamps must be strictly increasing")
        return cls(
            location=str(df["location"].iloc[0]), model=str(df["model"].iloc[0]),
            cutoff=pd.Timestamp(df["cutoff"].iloc[0]).tz_convert("UTC"),
            issued_at=pd.Timestamp(df["issued_at"].iloc[0]).tz_convert("UTC"),
            ts=ts, p10=df["p10"].to_numpy(np.float64), p50=df["p50"].to_numpy(np.float64),
            p90=df["p90"].to_numpy(np.float64), chunk=df["chunk"].to_numpy(np.int16),
        )


# --- where ---------------------------------------------------------------------------------------------


def default_model() -> str:
    from src.forecast.settings import load_settings

    return load_settings().model


def default_cache_dir() -> Path:
    from src.config import REPO_ROOT, load_raw_config

    raw = (load_raw_config().get("forecast") or {}).get("predict") or {}
    p = Path(str(raw.get("cache_dir", "data/cache/predictions")))
    return p if p.is_absolute() else REPO_ROOT / p


def _parquet_path(cache_dir: Path, model: str, location: str, cutoff: pd.Timestamp) -> Path:
    return Path(cache_dir) / model / location / f"{cutoff.tz_convert('UTC'):%Y%m%dT%H%M}.parquet"


# --- writing ---------------------------------------------------------------------------------------------


def write(paths: Iterable[PredictionPath], *, out: str, cache_dir: Path | None = None, conn=None) -> int:
    """Store paths ('db' or 'parquet'). Each (model, location, cutoff) replaces its previous path atomically."""
    paths = list(paths)
    if out == "parquet":
        folder = Path(cache_dir) if cache_dir is not None else default_cache_dir()
        for p in paths:
            path = _parquet_path(folder, p.model, p.location, p.cutoff)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".parquet.part")
            p.frame().to_parquet(tmp, index=False)
            tmp.replace(path)  # atomic: a reader sees the old path or the new one
    elif out == "db":
        _write_db(paths, conn)
    else:
        raise ValueError("out must be db or parquet")
    clear_cache()
    return sum(len(p) for p in paths)


def _write_db(paths: list[PredictionPath], conn=None) -> None:
    from src import db

    own = conn is None
    conn = conn or db.connect()
    try:
        for p in paths:
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("DELETE FROM price_predictions WHERE model = %s AND location = %s AND cutoff = %s",
                            (p.model, p.location, p.cutoff.to_pydatetime()))
                with cur.copy("COPY price_predictions (model, location, cutoff, ts, issued_at, chunk, p10, p50, p90) "
                              "FROM STDIN") as copy:
                    cutoff, issued = p.cutoff.to_pydatetime(), p.issued_at.to_pydatetime()
                    for t, c, a, b, d in zip(pd.to_datetime(p.ts, utc=True), p.chunk.tolist(), p.p10.tolist(),
                                             p.p50.tolist(), p.p90.tolist()):
                        copy.write_row((p.model, p.location, cutoff, t.to_pydatetime(), issued, int(c), a, b, d))
    finally:
        if own:
            conn.close()


# --- reading ----------------------------------------------------------------------------------------------

_cache: dict[tuple, tuple[float, PredictionPath | None]] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def load(location: str, *, max_cutoff: datetime | None = None, model: str | None = None, source: str | None = None,
         cache_dir: Path | None = None, refresh: bool = False, conn=None) -> PredictionPath | None:
    """The stored path for `location` with the latest cutoff at or before max_cutoff (any cutoff if None).
    Cached for CACHE_S per process (the coordinator and the API ask per run and per forecast lookup); a given
    `conn` (tests: a scratch schema) is read directly, uncached."""
    model = model or default_model()
    source = source or store.default_source()
    folder = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    cut = None if max_cutoff is None else pd.Timestamp(max_cutoff).tz_convert("UTC")
    if conn is not None:
        return _load_db(location, cut, model, conn)
    key = (location, None if cut is None else cut.value, model, source, str(folder))
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and hit[0] > now:
            return hit[1]
    path = _load_db(location, cut, model) if source == "db" else _load_parquet(location, cut, model, folder)
    with _lock:
        _cache[key] = (now + CACHE_S, path)
    return path


def _load_parquet(location: str, cut: pd.Timestamp | None, model: str, folder: Path) -> PredictionPath | None:
    files = sorted((folder / model / location).glob("*.parquet")) if (folder / model / location).is_dir() else []
    best = None
    for f in files:
        try:
            stamp = pd.Timestamp(f.stem.replace("T", " ")).tz_localize("UTC")
        except ValueError:
            continue
        if (cut is None or stamp <= cut) and (best is None or stamp > best[0]):
            best = (stamp, f)
    if best is None:
        return None
    df = pd.read_parquet(best[1])
    return PredictionPath.from_frame(df) if not df.empty else None


def _load_db(location: str, cut: pd.Timestamp | None, model: str, conn=None) -> PredictionPath | None:
    from src import db

    if conn is None:
        with db.connect() as own:
            return _load_db(location, cut, model, own)
    (table,) = conn.execute("SELECT to_regclass('price_predictions')").fetchone()
    if table is None:
        return None
    if cut is None:
        (cutoff,) = conn.execute("SELECT max(cutoff) FROM price_predictions WHERE model = %s AND location = %s",
                                 (model, location)).fetchone()
    else:
        (cutoff,) = conn.execute("SELECT max(cutoff) FROM price_predictions WHERE model = %s AND location = %s "
                                 "AND cutoff <= %s", (model, location, cut.to_pydatetime())).fetchone()
    if cutoff is None:
        return None
    rows = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM price_predictions WHERE model = %s AND location = %s "
                        "AND cutoff = %s ORDER BY ts", (model, location, cutoff)).fetchall()
    return PredictionPath.from_frame(pd.DataFrame(rows, columns=COLUMNS)) if rows else None


def load_all(locations: Sequence[str], cutoff: datetime, **kwargs) -> dict[str, PredictionPath]:
    """{location: path} for the locations that have a prediction usable at `cutoff` (see load)."""
    out = {}
    for loc in locations:
        p = load(loc, max_cutoff=cutoff, **kwargs)
        if p is not None and len(p):
            out[loc] = p
    return out


def ready(locations: Sequence[str], cutoff: datetime, **kwargs) -> bool:
    """Whether every location has a prediction whose cutoff is at or before `cutoff` and reaches past it."""
    cut = pd.Timestamp(cutoff).tz_convert("UTC")
    paths = load_all(locations, cutoff, **kwargs)
    return len(paths) == len(locations) and all(p.end > cut for p in paths.values())


# --- virtual forecasts -----------------------------------------------------------------------------------


def virtual_forecasts(path: PredictionPath, *, start: datetime, end: datetime | None = None,
                      horizon_steps: int = 96, every_minutes: int = 60) -> pd.DataFrame:
    """Hourly "virtual" forecasts sliced from a predicted path, in the forecasts-store row shape
    (src/forecast/store.py COLUMNS, series rt_price): issued at every hour T in [start, end) (default: the path's
    end), each covering [T, T + horizon) with the path's p10/p50/p90 (NaN past the path's end). They are not
    new model runs: every one is a slice of the prediction made at the cutoff, which saw only real prices."""
    series = "rt_price"
    t0 = pd.Timestamp(start).tz_convert("UTC")
    t1 = path.end if end is None else min(pd.Timestamp(end).tz_convert("UTC"), path.end)
    issues = pd.date_range(t0.ceil(f"{every_minutes}min"), t1, freq=f"{every_minutes}min", inclusive="left")
    if len(issues) == 0 or not len(path):
        return pd.DataFrame(columns=store.COLUMNS)
    pad = np.full(horizon_steps, np.nan)
    p10, p50, p90 = (np.concatenate([a.astype(np.float64), pad]) for a in (path.p10, path.p50, path.p90))
    first = int(path.ts[0])
    rows = []
    for t in issues:
        i = (int(t.value) - first) // STEP_NS
        if i < 0 or i >= len(path) or int(path.ts[i]) != int(t.value):
            continue  # a hole in the path (never made by the job): no forecast at this hour
        rows.append((t, path.location, series, path.model, t, STEP_MINUTES, p10[i:i + horizon_steps].astype(np.float32),
                     p50[i:i + horizon_steps].astype(np.float32), p90[i:i + horizon_steps].astype(np.float32)))
    df = pd.DataFrame(rows, columns=store.COLUMNS)
    if not df.empty:
        df["issued_at"] = pd.to_datetime(df["issued_at"], utc=True)
        df["start_ts"] = pd.to_datetime(df["start_ts"], utc=True)
    return df
