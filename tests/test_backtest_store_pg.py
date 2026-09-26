"""Backtest records in Postgres backtest_runs (postgres marker).

Skips unless DATABASE_URL points at a reachable Postgres. Each test runs in a throwaway schema
built from migrations/001_init.sql (search_path) and dropped afterwards, so real rows are
never touched. Prices still come from the committed snapshot (SERIES_BACKEND=snapshot).
"""
from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest

from src.backtest import service, store
from src.backtest.engine import local_midnight_utc
from src.backtest.results import build_results
from src.backtest.runner import backtest_settings, run_backtest
from src.config import REPO_ROOT, load_config
from src.feeds.memory import InMemoryPriceFeed
from src.feeds.parquet import load_rt_rows

pytestmark = pytest.mark.postgres
LOCS = tuple(load_config().fleet.locations)


@pytest.fixture
def pg(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set")
    try:
        conn = psycopg.connect(url, autocommit=True, connect_timeout=2)
    except Exception as exc:
        pytest.skip(f"Postgres is not reachable: {exc}")
    schema = f"bt_test_{uuid.uuid4().hex[:10]}"
    conn.execute(f'CREATE SCHEMA "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    conn.execute((REPO_ROOT / "migrations" / "001_init.sql").read_text())
    monkeypatch.setenv("DATABASE_URL", url + ("&" if "?" in url else "?") + f"options=-csearch_path%3D{schema}")
    monkeypatch.setenv("SERIES_BACKEND", "snapshot")
    monkeypatch.delenv("BACKTEST_RESULTS_DIR", raising=False)
    try:
        yield conn
    finally:
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def mini_backtest(out_dir, strategies=("bands", "forecast", "peak"), pitch=True, ceiling=True):
    start = local_midnight_utc(date(2025, 6, 1))
    rows = [r for r in load_rt_rows(years=(2025,), locations=LOCS)
            if start - timedelta(days=7) <= r.ts < start + timedelta(days=14)]
    settings = backtest_settings()
    settings["ceiling"].update(ratio_step=0.25, workers=1)
    return run_backtest(list(strategies), 2025, settings=settings, pitch=pitch, fleet_size=20, ceiling=ceiling,
                        feed=InMemoryPriceFeed.from_intervals(rows), out_dir=out_dir, log=lambda s: None)


def flags(conn) -> dict[str, bool]:
    return {str(r[0]): r[1] for r in conn.execute("SELECT run_id, is_pitch FROM backtest_runs")}


def test_pitch_set_round_trips_through_postgres(pg, tmp_path):
    outcome = mini_backtest(tmp_path)
    assert outcome.stored_in_db
    loaded = {r["strategy"]: r for r in store.load_pitch_records(2025)}
    assert set(loaded) == {"bands", "forecast", "peak", "hindsight"}
    for r in outcome.records:
        got = loaded[r["strategy"]]
        for key in ("run_id", "year", "fleet_total_usd", "per_battery_avg_usd", "per_location", "top_days", "metrics",
                    "notes", "started_at", "finished_at", "label"):
            assert got[key] == r[key], (r["strategy"], key)
        assert got["params"]["config"] == r["params"]["config"] and got["params"]["year"] == 2025
    assert loaded["forecast"]["fleet_total_usd"] is None and loaded["forecast"]["metrics"]["skipped"]
    assert all(flags(pg).values())
    assert store.load_run(outcome.records[0]["run_id"])["run_id"] == outcome.records[0]["run_id"]
    assert store.load_pitch_records(2024) == []


def test_a_new_pitch_run_replaces_only_its_own_strategy(pg, tmp_path):
    first = mini_backtest(tmp_path)
    old = {r["strategy"]: r["run_id"] for r in first.records}
    second = mini_backtest(tmp_path, strategies=("bands",), ceiling=False)
    new_bands = second.records[0]["run_id"]
    f = flags(pg)
    assert f[old["bands"]] is False and f[new_bands] is True
    assert f[old["peak"]] and f[old["hindsight"]] and f[old["forecast"]]
    pitch = {r["strategy"]: r["run_id"] for r in store.load_pitch_records(2025)}
    assert pitch == {**old, "bands": new_bands}
    # a run that is not for the pitch changes no flag
    mini_backtest(tmp_path, strategies=("peak",), pitch=False, ceiling=False)
    assert {k: v for k, v in flags(pg).items() if k in f} == f
    assert pg.execute("SELECT count(*) FROM backtest_runs").fetchone()[0] == 6
    assert pg.execute("SELECT count(*) FROM backtest_runs WHERE is_pitch").fetchone()[0] == 4


def test_a_skip_never_displaces_a_run_that_worked(pg, tmp_path):
    first = mini_backtest(tmp_path, strategies=("bands",), ceiling=False)
    good = dict(first.records[0], strategy="forecast", run_id=str(uuid.uuid4()))  # SYNTHETIC forecast run
    store.save_records([good], pitch=True, use_db=True, out_dir=tmp_path)
    later = mini_backtest(tmp_path, strategies=("forecast",), ceiling=False)  # not built: a skip marker
    marker = later.records[0]
    assert marker["metrics"]["skipped"]
    f = flags(pg)
    assert f[good["run_id"]] is True and f[marker["run_id"]] is False
    assert {r["strategy"]: r["run_id"] for r in store.load_pitch_records(2025)}["forecast"] == good["run_id"]
    # with nothing that worked before, the marker does become the pitch row (so the Results say why)
    pg.execute("DELETE FROM backtest_runs WHERE run_id = %s", (good["run_id"],))
    again = mini_backtest(tmp_path, strategies=("forecast",), ceiling=False)
    assert flags(pg)[again.records[0]["run_id"]] is True


def test_ceiling_curves_come_back_from_the_database_after_a_restart(pg, tmp_path):
    import shutil

    from src.backtest import ceiling
    from src.backtest.runner import build_context

    outcome = mini_backtest(tmp_path / "first")
    shutil.rmtree(tmp_path / "first")  # a fresh container: no cache/, no JSON
    start = local_midnight_utc(date(2025, 6, 1))
    rows = [r for r in load_rt_rows(years=(2025,), locations=LOCS)
            if start - timedelta(days=7) <= r.ts < start + timedelta(days=14)]
    settings = backtest_settings()
    settings["ceiling"].update(ratio_step=0.25, workers=1)
    ctx = build_context(2025, settings=settings, fleet_size=20, feed=InMemoryPriceFeed.from_intervals(rows))
    lines = []
    rec = ceiling.ceiling_record(ctx, out_dir=tmp_path / "second", log=lines.append)
    assert any("earlier ceiling run" in line for line in lines)
    assert rec["metrics"]["uncapped"] == outcome.ceiling["metrics"]["uncapped"]


def test_pitch_results_reads_the_database(pg, tmp_path):
    outcome = mini_backtest(tmp_path)
    (tmp_path / "pitch_2025.json").unlink()  # prove it is not the JSON fallback
    results = service.pitch_results(2025)
    assert results == build_results(outcome.records, year=2025)
    assert [s["strategy"] for s in results["strategies"]] == ["bands", "peak"]
    assert results["ceiling"]["per_battery_usd_year"] > 0
    # jsonb reorders object keys; the Results keep config order
    assert list(results["strategies"][0]["per_location_usd"]) == list(LOCS)
    assert any("in all four cities: Houston (7), Dallas–Fort Worth (8)" in a for a in results["assumptions"])


def test_a_cancelled_job_leaves_no_rows_and_a_finished_one_is_stored_by_the_job(pg, tmp_path, monkeypatch):
    """POST /backtest's jobs: the runner computes and stores nothing; the job stores the records in backtest_runs
    (as the pitch set) once it has finished, and never for a cancelled job."""
    import json
    import sys
    import time

    settings = backtest_settings()
    settings["ceiling"].update(ratio_step=0.25, workers=1)
    start = local_midnight_utc(date(2025, 6, 1))
    rows = [r for r in load_rt_rows(years=(2025,), locations=LOCS)
            if start - timedelta(days=7) <= r.ts < start + timedelta(days=14)]
    outcome = run_backtest(["bands", "peak"], 2025, settings=settings, pitch=True, fleet_size=20,
                           feed=InMemoryPriceFeed.from_intervals(rows), out_dir=tmp_path, log=lambda s: None,
                           save=False)
    assert pg.execute("SELECT count(*) FROM backtest_runs").fetchone()[0] == 0  # save=False: nothing
    records_path = tmp_path / "records.json"
    records_path.write_text(json.dumps(store.clean(outcome.records)))
    monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(tmp_path / "results"))

    def stand_in(delay_s):  # writes what the real runner writes, after `delay_s`
        def command(names, year, status_file, records_file=None):
            status = {"run_ids": outcome.run_ids, "skipped": {}, "error": None,
                      "ceiling_run_id": outcome.ceiling["run_id"]}
            code = (f"import shutil, sys, time; time.sleep({delay_s}); "
                    f"shutil.copyfile({str(records_path)!r}, {records_file!r}); "
                    f"open({status_file!r}, 'w').write({json.dumps(status)!r})")
            return [sys.executable, "-c", code]
        return command

    monkeypatch.setattr(service, "runner_command", stand_in(20.0))
    job_id = service.start_backtest(["bands", "peak"], 2025)
    deadline = time.monotonic() + 10
    while not service._jobs[job_id]["pid"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert service.cancel_job(job_id) == {"status": "cancelled"}
    assert service._jobs[job_id]["settled"].wait(10)
    assert pg.execute("SELECT count(*) FROM backtest_runs").fetchone()[0] == 0
    assert not (tmp_path / "results").exists()
    monkeypatch.setattr(service, "runner_command", stand_in(0.0))
    job_id = service.start_backtest(["bands", "peak"], 2025)
    assert service._jobs[job_id]["settled"].wait(20)
    assert service.get_job(job_id)["status"] == "done"
    stored = {r["run_id"] for r in store.load_pitch_records(2025)}
    assert stored == {r["run_id"] for r in outcome.records}
    with pytest.raises(service.JobNotRunning):
        service.cancel_job(job_id)


def _synthetic_forecast_run(year: int = 2025) -> dict:
    """A forecast run on SYNTHETIC forecast books (test doubles, not TimesFM) over the mini window."""
    import numpy as np

    from src.backtest.runner import build_context, run_strategy
    from src.forecast.provider import ForecastBook
    from src.strategy.forecast import ForecastStrategy

    start = local_midnight_utc(date(2025, 6, 1))
    rows = [r for r in load_rt_rows(years=(2025,), locations=LOCS)
            if start - timedelta(days=7) <= r.ts < start + timedelta(days=14)]
    settings = backtest_settings()
    settings["ceiling"].update(ratio_step=0.25, workers=1)
    ctx = build_context(year, settings=settings, fleet_size=20, feed=InMemoryPriceFeed.from_intervals(rows))
    first = int(ctx.series[LOCS[0]].replayed[0].ts.timestamp()) - 3600
    n = len(ctx.series[LOCS[0]].replayed) // 12 + 48
    issued = (first + np.arange(n, dtype=np.int64) * 3600) * 10**9
    shape = np.tile(np.linspace(20.0, 80.0, 96, dtype=np.float32), (n, 1))
    books = {loc: ForecastBook(loc, "rt_price", "synthetic", 15, issued, issued.copy(), shape - 5, shape, shape + 5)
             for loc in LOCS}
    return run_strategy("forecast", ctx, lag0=False,
                        strategies={loc: ForecastStrategy(books, duration_h=2.0) for loc in LOCS})


def test_ai_gains_reads_the_database(pg, tmp_path, monkeypatch):
    """GET /results/ai-gains from backtest_runs: the same object as from the JSON export, and runs stored before
    metrics.insights existed (here: copies for another year, without it) come without the insight fields."""
    import json

    outcome = mini_backtest(tmp_path)  # 2025 pitch set: bands, peak, the ceiling (forecast: a skip marker)
    forecast = _synthetic_forecast_run()
    store.save_records([forecast], pitch=True, use_db=True, out_dir=tmp_path)
    old = []
    for r in outcome.records + [forecast]:  # a 2024 set as stored before insights: copies of the 2025 rows
        if r["metrics"].get("skipped"):
            continue
        metrics = {k: v for k, v in r["metrics"].items() if k != "insights"}
        old.append({**r, "run_id": str(uuid.uuid4()), "year": 2024, "metrics": metrics,
                    "params": {**r["params"], "year": 2024}})
    store.save_records(old, pitch=False, use_db=True, out_dir=tmp_path)
    slim = store.load_gains_records()
    assert {r["strategy"] for r in slim} == {"bands", "forecast", "peak", "hindsight"}  # no skip marker
    assert all(r["insights"] is None for r in slim if r["year"] == 2024)
    from_db = service.ai_gains()
    assert [(y["year"], y["is_pitch"]) for y in from_db["years"]] == [(2025, True), (2024, False)]
    y25, y24 = from_db["years"]
    assert y25["strategies"]["forecast"]["spike_events"] == forecast["metrics"]["insights"]["spike_events"]
    assert "surplus_mwh" in y25["strategies"]["bands"]
    assert "surplus_mwh" not in y24["strategies"]["bands"] and "share_of_best" in y24["strategies"]["bands"]
    assert y24["uplift"] == y25["uplift"]  # the same numbers, stored again for 2024
    # the database and the JSON export give the same answer
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(tmp_path))
    assert json.dumps(service.ai_gains(), sort_keys=True) == json.dumps(from_db, sort_keys=True)


def test_each_year_has_its_own_pitch_set_and_2026_splits_its_money(pg, tmp_path, monkeypatch):
    """A 2026 pitch set (real prices to real_until_utc, then a SYNTHETIC prediction: a test double written where the
    prediction job writes, no TimesFM) is stored next to 2025's without touching it; the database serves its split."""
    import numpy as np
    import pandas as pd

    from src.data import years
    from src.forecast import predictions

    cutoff = pd.Timestamp("2026-09-20T05:00Z")  # the committed 2026 snapshot's real_until_utc
    n = 3 * 96
    ts = cutoff.value + np.arange(n, dtype=np.int64) * 15 * 60 * 10**9
    hour = pd.to_datetime(ts, utc=True).tz_convert("America/Chicago").hour.to_numpy() + 0.0
    wave = np.round(50.0 + 30.0 * np.sin((hour - 13.0) / 24.0 * 2 * np.pi), 2)
    monkeypatch.setenv("FORECAST_SOURCE", "parquet")
    monkeypatch.setattr(predictions, "default_cache_dir", lambda: tmp_path / "predictions")
    years.clear_cache()
    predictions.clear_cache()
    predictions.write([predictions.PredictionPath(loc, "timesfm-3.0", cutoff, pd.Timestamp("2026-09-26T12:00Z"), ts,
                                                  wave - 8, wave, wave + 8, np.zeros(n, dtype=np.int16))
                       for loc in LOCS], out="parquet", cache_dir=tmp_path / "predictions")
    try:
        first = mini_backtest(tmp_path)  # the 2025 pitch set
        flags25 = flags(pg)
        start = local_midnight_utc(date(2026, 9, 10))
        rows = [r for r in load_rt_rows(years=(2026,), locations=LOCS) if r.ts >= start - timedelta(days=7)]
        settings = backtest_settings()
        settings["ceiling"].update(ratio_step=0.25, workers=1)
        out26 = run_backtest(["bands", "peak"], 2026, settings=settings, pitch=True, fleet_size=20,
                             feed=InMemoryPriceFeed.from_intervals(rows), out_dir=tmp_path, log=lambda s: None)
        assert out26.stored_in_db
        now = flags(pg)
        assert all(now[run_id] == flag for run_id, flag in flags25.items())  # 2025's pitch set untouched
        assert {r["run_id"] for r in store.load_pitch_records(2025)} == {r["run_id"] for r in first.records}
        assert {r["run_id"] for r in store.load_pitch_records(2026)} == {r["run_id"] for r in out26.records}
        (tmp_path / "pitch_2026.json").unlink()  # prove it is the database that answers
        results = service.pitch_results(2026)
        assert results == build_results(out26.records, year=2026)
        assert results["real_until_utc"] == "2026-09-20T05:00:00Z"
        for s in results["strategies"] + [results["ceiling"]]:
            assert round(s["real_per_battery_usd"] + s["projected_per_battery_usd"], 2) == s["per_battery_usd_year"]
            assert s["projected_per_battery_usd"] != 0.0
        slim = {r["run_id"]: r for r in store.load_gains_records()}
        for r in out26.records:
            got = slim[r["run_id"]]["split"]
            assert got["real_until_utc"] == "2026-09-20T05:00:00Z"
            assert got["real_per_battery_usd"] == r["metrics"]["split"]["real_per_battery_usd"]
        assert all(slim[r["run_id"]]["split"]["real_until_utc"] is None for r in first.records
                   if r["fleet_total_usd"] is not None)
    finally:
        years.clear_cache()
        predictions.clear_cache()
