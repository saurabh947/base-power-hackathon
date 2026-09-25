"""Backtest records in Postgres (backtest_runs, migrations/001_init.sql) and as JSON files.

A record is one strategy's full-year run, or the ceiling (strategy 'hindsight'), or a skip
marker (metrics.skipped = true, with metrics.reason) for a requested strategy that could not
run. Columns: run_id, started_at, finished_at, strategy, params (config snapshot, battery
ranges, year, label), fleet_total_usd, per_battery_avg_usd, per_location, top_days, metrics,
notes, is_pitch.

is_pitch marks the Results screen's set: at most one row per (year, strategy). Saving with
pitch=True clears the flag on that strategy's previous row for the year and sets it on the new
one, in one transaction for the whole batch.

JSON (always written; the directory is data/results/ unless configured otherwise):
  runs/<run_id>.json       every record
  pitch_<year>_runs.json   the pitch set's records (JSON-only mode merges into it)
  pitch_<year>.json        the Results object served by GET /results/pitch (small)
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from src.config import REPO_ROOT

COLUMNS = ("run_id", "started_at", "finished_at", "strategy", "params", "fleet_total_usd", "per_battery_avg_usd",
           "per_location", "top_days", "metrics", "notes", "is_pitch")


def results_dir(out_dir: str | Path | None = None, settings: dict | None = None) -> Path:
    """out_dir, else env BACKTEST_RESULTS_DIR, else config backtest.results_dir (relative to the repo)."""
    if out_dir is None:
        out_dir = os.environ.get("BACKTEST_RESULTS_DIR")
    if out_dir is None:
        if settings is None:
            from src.backtest.runner import backtest_settings

            settings = backtest_settings()
        out_dir = settings.get("results_dir", "data/results")
    path = Path(out_dir)
    return path if path.is_absolute() else REPO_ROOT / path


def db_configured() -> bool:
    return bool(os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST"))


def clean(value):
    """JSON-safe: non-finite floats become None (Postgres jsonb rejects NaN)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


@dataclass
class Saved:
    in_db: bool
    paths: list[str]


def save_records(
    records: Sequence[dict],
    *,
    pitch: bool,
    use_db: bool | None = None,
    out_dir: str | Path | None = None,
    settings: dict | None = None,
    cfg=None,
    conn=None,
) -> Saved:
    """JSON first (so nothing is lost if the database is down), then Postgres when configured."""
    records = [clean(r) for r in records]
    paths = write_json(records, pitch=pitch, out_dir=out_dir, settings=settings)
    use_db = db_configured() if use_db is None else use_db
    if use_db:
        insert_records(records, pitch=pitch, conn=conn)
    return Saved(bool(use_db), paths)


# --- JSON --------------------------------------------------------------------------------------


def write_json(records: Sequence[dict], *, pitch: bool, out_dir=None, settings: dict | None = None) -> list[str]:
    from src.backtest.results import build_results

    base = results_dir(out_dir, settings)
    (base / "runs").mkdir(parents=True, exist_ok=True)
    paths = []
    for r in records:
        path = base / "runs" / f"{r['run_id']}.json"
        _write(path, r)
        paths.append(_rel(path))
    if pitch:
        for year in sorted({r["year"] for r in records}):
            merged = {r["strategy"]: r for r in load_pitch_records_json(year, base)}
            merged.update({r["strategy"]: r for r in records if r["year"] == year})
            runs_path = base / f"pitch_{year}_runs.json"
            _write(runs_path, list(merged.values()))
            results_path = base / f"pitch_{year}.json"
            _write(results_path, build_results(list(merged.values()), year=year, settings=settings))
            paths += [_rel(runs_path), _rel(results_path)]
    return paths


def load_pitch_records_json(year: int, out_dir=None) -> list[dict]:
    path = results_dir(out_dir) / f"pitch_{year}_runs.json"
    return json.loads(path.read_text()) if path.exists() else []


def read_pitch_results_json(year: int, out_dir=None) -> dict | None:
    path = results_dir(out_dir) / f"pitch_{year}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _write(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, allow_nan=False))
    tmp.replace(path)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# --- Postgres ----------------------------------------------------------------------------------


def _connect(conn):
    if conn is not None:
        return conn, False
    from src.db import connect

    return connect(connect_timeout=5), True


def insert_records(records: Iterable[dict], *, pitch: bool, conn=None) -> None:
    """Insert records; with pitch=True they replace the previous pitch row of each (year, strategy)."""
    from psycopg.types.json import Jsonb

    conn, owned = _connect(conn)
    try:
        with conn.transaction():
            for r in records:
                if pitch:
                    conn.execute(
                        "UPDATE backtest_runs SET is_pitch = false "
                        "WHERE is_pitch AND strategy = %s AND params->>'year' = %s",
                        (r["strategy"], str(r["year"])),
                    )
                params = dict(r["params"])
                params.setdefault("year", r["year"])
                params["label"] = r.get("label")
                conn.execute(
                    f"INSERT INTO backtest_runs ({', '.join(COLUMNS)}) "
                    f"VALUES ({', '.join(['%s'] * len(COLUMNS))})",
                    (
                        r["run_id"], _ts(r["started_at"]), _ts(r["finished_at"]), r["strategy"], Jsonb(params),
                        r["fleet_total_usd"], r["per_battery_avg_usd"],
                        Jsonb(r["per_location"]) if r["per_location"] is not None else None,
                        Jsonb(r["top_days"]) if r["top_days"] is not None else None,
                        Jsonb(r["metrics"]), r.get("notes"), bool(pitch),
                    ),
                )
    finally:
        if owned:
            conn.close()


def load_pitch_records(year: int, *, conn=None) -> list[dict]:
    """The pitch set for a year: the flagged row of each strategy (newest wins if several)."""
    conn, owned = _connect(conn)
    try:
        rows = conn.execute(
            "SELECT run_id, started_at, finished_at, strategy, params, fleet_total_usd, per_battery_avg_usd, "
            "per_location, top_days, metrics, notes FROM backtest_runs "
            "WHERE is_pitch AND params->>'year' = %s ORDER BY finished_at DESC NULLS LAST, started_at DESC",
            (str(year),),
        ).fetchall()
    finally:
        if owned:
            conn.close()
    out: dict[str, dict] = {}
    for row in rows:
        record = row_to_record(row)
        out.setdefault(record["strategy"], record)
    return list(out.values())


def load_run(run_id: str, *, conn=None) -> dict | None:
    conn, owned = _connect(conn)
    try:
        row = conn.execute(
            "SELECT run_id, started_at, finished_at, strategy, params, fleet_total_usd, per_battery_avg_usd, "
            "per_location, top_days, metrics, notes FROM backtest_runs WHERE run_id = %s",
            (run_id,),
        ).fetchone()
    finally:
        if owned:
            conn.close()
    return row_to_record(row) if row else None


def row_to_record(row) -> dict:
    (run_id, started, finished, strategy, params, total, per_battery, per_location, top_days, metrics, notes) = row
    params = params or {}
    return {
        "run_id": str(run_id),
        "strategy": strategy,
        "label": params.get("label") or strategy,
        "year": int(params.get("year")) if params.get("year") is not None else None,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "fleet_total_usd": total,
        "per_battery_avg_usd": per_battery,
        "per_location": per_location,
        "top_days": top_days,
        "metrics": metrics or {},
        "params": params,
        "notes": notes,
    }


def _ts(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _iso(value) -> str | None:
    if value is None:
        return None
    from datetime import timezone

    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
