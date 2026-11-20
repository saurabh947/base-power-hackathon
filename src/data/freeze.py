"""Freeze the database's derived data into files, so the backend runs without a database (data/frozen).

The backend reads prices from data/snapshot already. What only a database held, it reads from files once the
image sets FORECAST_CACHE_DIR, PREDICTION_CACHE_DIR and BACKTEST_RESULTS_DIR (the Dockerfile points them here):

  forecasts/<model>/<series>/<place>/<first issue>.parquet  TimesFM rolling forecasts (rt_price per city, demand for
                                                            the grid), one file per place and year
  predictions/<model>/<city>/<cutoff>.parquet               the AI's prediction after a year's real_until_utc
  results/runs/<run_id>.json                                every stored full-year run
  results/pitch_<year>_runs.json, results/pitch_<year>.json the Results screen's set for each year

Run it against a database holding the cloud data (for fleet-sim-db: its SQL export, restored locally):
    DATABASE_URL=postgresql://postgres@localhost:5442/fleetsim_cloud uv run python -m src.data.freeze --out data/frozen

Every file is read back and compared with the database before the command reports success. Forecast arrays are
Postgres `real` (float32), so they round-trip exactly.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

UTC = timezone.utc
DEFAULT_SINCE = "2024-12-24T00:00:00Z"  # the last week of 2024: enough for a replay from Jan 1, 2025


def freeze_forecasts(out: Path, model: str, places: list[tuple[str, str]], since: datetime) -> dict[str, int]:
    """Forecasts of each (series, location) issued from `since` on, one parquet file per place and UTC year."""
    from src.forecast import store

    folder = out / "forecasts"
    counts = {}
    for series, loc in places:
        df = store.read_db(model, series, loc, start=since)
        if df.empty:
            raise SystemExit(f"no {model} {series} forecasts for {loc} since {since:%Y-%m-%d}")
        sink = store.ParquetSink(folder)
        for _, part in df.groupby(df["issued_at"].dt.year, sort=True):
            sink.write([
                store.ForecastRow(pd.Timestamp(r.issued_at), r.location, r.series, r.model, pd.Timestamp(r.start_ts),
                                  int(r.step_minutes), np.asarray(r.p10, np.float32), np.asarray(r.p50, np.float32),
                                  np.asarray(r.p90, np.float32))
                for r in part.itertuples(index=False)
            ])
        back = store.read_parquet(folder, model, series, loc, start=since)
        _same_forecasts(df, back, f"{series} {loc}")
        counts[f"{series} {loc}"] = len(df)
    return counts


def _same_forecasts(db: pd.DataFrame, files: pd.DataFrame, loc: str) -> None:
    if len(db) != len(files):
        raise SystemExit(f"{loc}: {len(db)} forecasts in the database but {len(files)} in the files")
    db = db.sort_values("issued_at").reset_index(drop=True)
    for col in ("issued_at", "start_ts"):
        if not (pd.to_datetime(db[col], utc=True).values == pd.to_datetime(files[col], utc=True).values).all():
            raise SystemExit(f"{loc}: {col} differs between the database and the files")
    if not (db["step_minutes"].astype(int).values == files["step_minutes"].astype(int).values).all():
        raise SystemExit(f"{loc}: step_minutes differs between the database and the files")
    for q in ("p10", "p50", "p90"):
        for a, b in zip(db[q], files[q]):
            if not np.array_equal(np.asarray(a, np.float32), np.asarray(b, np.float32)):
                raise SystemExit(f"{loc}: {q} values differ between the database and the files")


def freeze_predictions(out: Path, model: str, locations: list[str]) -> dict[str, str]:
    """For each configured year whose real prices end early, the prediction the replay uses (the latest cutoff
    at or before that year's real_until_utc, read from data/snapshot as the backend does)."""
    from src.data import years
    from src.forecast import predictions

    folder = out / "predictions"
    written = {}
    for year in years.configured_years():
        until = years.real_until(year)
        if until is None:
            continue
        for loc in locations:
            path = predictions.load(loc, max_cutoff=until, model=model, source="db", refresh=True)
            if path is None:
                raise SystemExit(f"no {model} prediction for {loc} with a cutoff at or before {until:%Y-%m-%d %H:%M}Z")
            predictions.write([path], out="parquet", cache_dir=folder)
            back = predictions.load(loc, max_cutoff=until, model=model, source="parquet", cache_dir=folder, refresh=True)
            if back is None or back.cutoff != path.cutoff or not all(
                    np.array_equal(getattr(back, f), getattr(path, f)) for f in ("ts", "p10", "p50", "p90", "chunk")):
                raise SystemExit(f"{loc}: the prediction read back from the files differs from the database's")
            written[f"{year} {loc}"] = f"{path.cutoff:%Y-%m-%dT%H:%MZ} ({len(path)} points)"
    return written


def freeze_results(out: Path) -> dict[str, int]:
    """Every backtest_runs row as runs/<run_id>.json, then each year's pitch set as the Results files."""
    from src import db
    from src.backtest import store

    base = out / "results"
    (base / "runs").mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT run_id, started_at, finished_at, strategy, params, fleet_total_usd, per_battery_avg_usd, "
            "per_location, top_days, metrics, notes, is_pitch FROM backtest_runs ORDER BY finished_at, run_id"
        ).fetchall()
        pitch_years = sorted({int((row[4] or {}).get("year")) for row in rows
                              if row[11] and (row[4] or {}).get("year") is not None})
        for row in rows:
            record = store.clean(store.row_to_record(row[:11]))
            store._write(base / "runs" / f"{record['run_id']}.json", record)
        for year in pitch_years:
            store.write_json(store.load_pitch_records(year, conn=conn), pitch=True, out_dir=base)
    return {"runs": len(rows), **{f"pitch {year}": 1 for year in pitch_years}}


def main(argv: list[str] | None = None) -> int:
    from src.forecast import store
    from src.forecast.settings import load_settings

    ap = argparse.ArgumentParser(prog="python -m src.data.freeze", description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("data/frozen"))
    ap.add_argument("--since", default=DEFAULT_SINCE, help="first forecast issue to keep (UTC)")
    ap.add_argument("--replace", action="store_true", help="delete forecasts/, predictions/ and results/ under --out first")
    args = ap.parse_args(argv)
    if not store.database_configured():
        print("error: set DATABASE_URL (or DB_HOST) to the database to freeze", file=sys.stderr)
        return 2
    out = args.out.resolve()
    for part in ("forecasts", "predictions", "results"):
        if (out / part).exists():
            if not args.replace:
                print(f"error: {out / part} exists; pass --replace to rebuild it", file=sys.stderr)
                return 2
            shutil.rmtree(out / part)
    settings = load_settings()
    locations = list(settings.locations)
    places = [("rt_price", loc) for loc in locations] + [("demand", settings.demand_location)]
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00")).astimezone(UTC)
    summary = {
        "model": settings.model,
        "since": f"{since:%Y-%m-%dT%H:%M:%SZ}",
        "forecasts": freeze_forecasts(out, settings.model, places, since),
        "predictions": freeze_predictions(out, settings.model, locations),
        "results": freeze_results(out),
    }
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
