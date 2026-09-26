"""Results object, JSON store, the API bridge (service) and the CLI.

A mini backtest (20 batteries, 3 weeks of real June 2025 prices from the snapshot) produces
real records quickly. No database here: tests/test_backtest_store_pg.py covers Postgres.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import date, timedelta

import pytest

from src.backtest import runner, service, store
from src.backtest.engine import local_midnight_utc
from src.backtest.results import build_results
from src.backtest.runner import backtest_settings, run_backtest
from src.config import REPO_ROOT, load_config
from src.feeds.memory import InMemoryPriceFeed
from src.feeds.parquet import load_rt_rows

LOCS = tuple(load_config().fleet.locations)


def mini_feed(first="2025-06-01", days=14, warmup=7) -> InMemoryPriceFeed:
    start = local_midnight_utc(date.fromisoformat(first))
    rows = [r for r in load_rt_rows(years=(2025,), locations=LOCS)
            if start - timedelta(days=warmup) <= r.ts < start + timedelta(days=days)]
    return InMemoryPriceFeed.from_intervals(rows)


def mini_settings():
    s = backtest_settings()
    s["ceiling"].update(ratio_step=0.25, workers=1)
    return s


def mini_backtest(out_dir, strategies=("bands", "forecast", "peak"), pitch=True, **kw):
    return run_backtest(list(strategies), 2025, settings=mini_settings(), pitch=pitch, use_db=False, fleet_size=20,
                        feed=mini_feed(), out_dir=out_dir, log=lambda s: None, **kw)


@pytest.fixture(scope="module")
def mini(tmp_path_factory):
    out = tmp_path_factory.mktemp("results")
    return out, mini_backtest(out)


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)


def api_example() -> dict:
    text = (REPO_ROOT / "docs" / "API.md").read_text()
    block = re.search(r"\*\*Results\*\* \(`GET /results/pitch`\)\s*```json\s*(\{.*?\})\s*```", text, re.S)
    return json.loads(block.group(1))


# --- the Results contract ---------------------------------------------------------------------


def test_results_keys_are_exactly_the_api_contract(mini):
    out, outcome = mini
    results = build_results(outcome.records, year=2025, settings=mini_settings())
    example = api_example()
    assert set(results) == set(example)
    assert set(results["ceiling"]) == set(example["ceiling"])
    for s in results["strategies"]:
        assert set(s) == set(example["strategies"][0])
        assert set(s["per_location_usd"]) == set(LOCS)
        for d in s["top_days"]:
            assert set(d) == set(example["strategies"][0]["top_days"][0])
    assert all(isinstance(line, str) and line for line in results["assumptions"])
    json.dumps(results, allow_nan=False)


def test_results_numbers_come_from_the_records(mini):
    _, outcome = mini
    results = build_results(outcome.records, year=2025, settings=mini_settings())
    by_name = {r["strategy"]: r for r in outcome.records}
    assert [s["strategy"] for s in results["strategies"]] == ["bands", "peak"]  # config order; forecast skipped
    bands = results["strategies"][0]
    assert bands["is_headline"] and not results["strategies"][1]["is_headline"]  # forecast absent -> bands
    assert bands["run_id"] == by_name["bands"]["run_id"]
    assert bands["fleet_usd_year"] == round(by_name["bands"]["fleet_total_usd"], 2)
    assert bands["per_battery_usd_year"] == round(by_name["bands"]["fleet_total_usd"] / 20, 2)
    assert bands["top_days"][0]["usd"] == round(by_name["bands"]["top_days"][0]["usd"], 2)
    assert results["fleet_size"] == 20
    assert results["ceiling"]["per_battery_usd_year"] == round(by_name["hindsight"]["per_battery_avg_usd"], 2)
    assert results["ceiling"]["per_battery_usd_year"] > max(s["per_battery_usd_year"] for s in results["strategies"])
    text = " ".join(results["assumptions"])
    assert "ERCOT, the Texas grid operator" in text and "battery wear" in text
    assert "isn't in these results" in text and "AI forecast (TimesFM-3)" in text  # the skip is explained
    assert "15 minutes late" in text and "If prices were known instantly" in text
    assert "Held to 1 full cycle a day" in text
    assert "Prices were missing" in text  # the mini feed covers 3 weeks of the year
    assert "TimesFM 2.5" not in text  # licence note only when the forecast ran
    # the fleet line names the four cities (src.sim.geo) with their batteries, in config order
    assert "in all four cities: Houston (7), Dallas–Fort Worth (8), San Antonio (3) and Austin (2)." in text
    # no forecasts cached here: the skip names every city that has none, in plain words
    assert "AI forecast (TimesFM-3) method isn't in these results: its AI price forecasts for Houston, " \
           "Dallas–Fort Worth, San Antonio and Austin aren't ready for 2025 yet." in text


def test_forecast_is_the_headline_when_it_ran(mini):
    _, outcome = mini
    records = [dict(r) for r in outcome.records if r["strategy"] != "forecast"]
    fake = dict(next(r for r in records if r["strategy"] == "bands"), strategy="forecast", run_id="f-1")  # SYNTHETIC
    results = build_results(records + [fake], year=2025, settings=mini_settings())
    heads = {s["strategy"]: s["is_headline"] for s in results["strategies"]}
    assert heads == {"bands": False, "forecast": True, "peak": False}
    assert [s["strategy"] for s in results["strategies"]] == ["bands", "forecast", "peak"]
    assert any("TimesFM 2.5" in line for line in results["assumptions"])


def test_records_hold_the_required_outputs(mini):
    _, outcome = mini
    bands = next(r for r in outcome.records if r["strategy"] == "bands")
    m = bands["metrics"]
    for key in ("per_battery_usd_year", "fleet_usd_year", "usd_per_kw_year", "usd_per_kwh_year", "cycles_per_year",
                "sold_kwh", "bought_kwh", "per_location", "top_days", "lag0", "coverage", "monthly_usd"):
        assert key in m, key
    assert m["lag_ticks"] == 3 and m["lag0"]["lag_ticks"] == 0
    assert len(bands["top_days"]) == 10 and bands["top_days"][0]["usd"] >= bands["top_days"][-1]["usd"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", bands["top_days"][0]["date_local"])
    p = bands["params"]
    assert p["year"] == 2025 and p["fleet"]["battery_ranges"]["power_kw"] == [5.0, 11.5]
    assert p["config"]["seed"] == 42 and p["config"]["backtest"]["headline"] == ["forecast", "bands"]
    assert p["fleet"]["size"] == 20 and p["lag_ticks"] == 3
    forecast = next(r for r in outcome.records if r["strategy"] == "forecast")
    # forecast is built now, but no forecasts are cached in a bare checkout: the coverage guard skips it
    assert forecast["metrics"]["skipped"] and "forecasts cover" in forecast["metrics"]["reason"]
    assert all(f"at {loc}" in forecast["metrics"]["reason"] for loc in LOCS)  # every city is checked
    assert outcome.skipped == {"forecast": forecast["metrics"]["reason"]}
    assert set(outcome.run_ids) == {r["run_id"] for r in outcome.records if r["strategy"] in ("bands", "peak",
                                                                                               "hindsight")}


# --- JSON store ------------------------------------------------------------------------------


def test_json_export_and_pitch_merge(mini, tmp_path):
    out, outcome = mini
    for r in outcome.records:
        assert (out / "runs" / f"{r['run_id']}.json").exists()
    assert json.loads((out / "pitch_2025.json").read_text()) == build_results(
        outcome.records, year=2025, settings=mini_settings())
    # JSON-only mode: a later pitch job for one strategy replaces just that strategy
    first = mini_backtest(tmp_path, strategies=("bands",), ceiling=True)
    second = mini_backtest(tmp_path, strategies=("peak",), ceiling=False)
    merged = {r["strategy"]: r["run_id"] for r in store.load_pitch_records_json(2025, tmp_path)}
    assert merged == {"bands": first.records[0]["run_id"], "hindsight": first.ceiling["run_id"],
                      "peak": second.records[0]["run_id"]}
    results = store.read_pitch_results_json(2025, tmp_path)
    assert [s["strategy"] for s in results["strategies"]] == ["bands", "peak"]
    # a non-pitch run leaves the pitch set alone
    mini_backtest(tmp_path, strategies=("bands",), pitch=False, ceiling=False)
    assert {r["strategy"]: r["run_id"] for r in store.load_pitch_records_json(2025, tmp_path)} == merged


def test_a_skip_never_displaces_a_run_that_worked(mini, tmp_path):
    import uuid

    _, outcome = mini
    bands = next(r for r in outcome.records if r["strategy"] == "bands")
    good = dict(bands, strategy="forecast", label="AI forecast (TimesFM-3)", run_id=str(uuid.uuid4()))  # SYNTHETIC
    store.save_records([good], pitch=True, use_db=False, out_dir=tmp_path, settings=mini_settings())
    later = mini_backtest(tmp_path, strategies=("bands", "forecast"), ceiling=False)  # forecast is skipped now
    assert later.skipped
    pitch = {r["strategy"]: r["run_id"] for r in store.load_pitch_records_json(2025, tmp_path)}
    assert pitch["forecast"] == good["run_id"]
    results = store.read_pitch_results_json(2025, tmp_path)
    assert next(s for s in results["strategies"] if s["is_headline"])["strategy"] == "forecast"
    assert not any("isn't in these results" in a for a in results["assumptions"])


def test_ceiling_reuses_curves_from_the_pitch_set_after_a_restart(mini, tmp_path):
    from src.backtest import ceiling
    from src.backtest.runner import build_context

    out, outcome = mini
    (tmp_path / "pitch_2025_runs.json").write_text((out / "pitch_2025_runs.json").read_text())  # no cache/ dir
    ctx = build_context(2025, settings=mini_settings(), fleet_size=20, feed=mini_feed())
    lines = []
    rec = ceiling.ceiling_record(ctx, out_dir=tmp_path, log=lines.append)
    assert any("earlier ceiling run" in line for line in lines) and not any("solving" in line for line in lines)
    assert rec["metrics"]["uncapped"] == outcome.ceiling["metrics"]["uncapped"]
    assert (tmp_path / "cache" / f"ceiling_curves_{rec['metrics']['cache_key']}.json").exists()


def test_clean_makes_records_json_safe():
    assert store.clean({"a": float("nan"), "b": [1.0, float("inf")], "c": 2}) == {"a": None, "b": [1.0, None], "c": 2}


# --- the API bridge ------------------------------------------------------------------------------


def test_pitch_results_reads_the_json_export_without_a_database(mini, monkeypatch):
    out, outcome = mini
    monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(out))
    assert service.pitch_results(2025) == json.loads((out / "pitch_2025.json").read_text())
    with pytest.raises(service.ResultsNotFound):
        service.pitch_results(2019)


def test_pitch_results_falls_back_to_json_when_the_database_fails(mini, monkeypatch):
    out, _ = mini
    monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(out))
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@localhost:1/none")

    def boom(year, conn=None):
        raise OSError("database down")

    monkeypatch.setattr(store, "load_pitch_records", boom)
    assert service.pitch_results(2025)["year"] == 2025


@pytest.mark.parametrize("strategies,year", [([], 2025), (["bands", "solver"], 2025), ("bands", 2025),
                                             (["bands"], 1999), (["bands"], 2999), (["bands"], True)])
def test_start_backtest_rejects_bad_requests(strategies, year):
    with pytest.raises(ValueError):
        service.start_backtest(strategies, year)


def test_runner_command_is_the_cli():
    cmd = service.runner_command(["bands", "peak"], 2025, "/tmp/s.json")
    assert cmd[1:] == ["-m", "src.backtest.runner", "--strategies", "bands,peak", "--year", "2025", "--pitch",
                       "--status-file", "/tmp/s.json"]
    # the API's jobs: the runner stores nothing and hands the records over (a cancel can't leave a trace)
    assert service.runner_command(["bands"], 2025, "/tmp/s.json", "/tmp/r.json")[-2:] == ["--records-out",
                                                                                          "/tmp/r.json"]


def fake_runner(delay_s: float, status: dict, exit_code: int):
    """Stands in for the CLI subprocess (a real run takes a minute): sleeps, writes the status file (and no
    records: the job has nothing to store)."""

    def command(names, year, status_file, records_file=None):
        records = f"open({records_file!r}, 'w').write('[]'); " if records_file else ""
        code = (f"import json, sys, time; time.sleep({delay_s}); {records}"
                f"open({status_file!r}, 'w').write(json.dumps({status!r})); sys.exit({exit_code})")
        return [sys.executable, "-c", code]

    return command


def wait_done(job_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.get_job(job_id)
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_one_job_at_a_time_then_done(monkeypatch):
    status = {"run_ids": ["r-bands", "r-peak"], "skipped": {"forecast": "not built"}, "error": None,
              "ceiling_run_id": "r-ceiling"}
    monkeypatch.setattr(service, "runner_command", fake_runner(0.6, status, 0))
    seen = []
    done_event = threading.Event()
    service.add_listener(lambda job_id, job: (seen.append((job_id, job)), done_event.set()))
    try:
        job_id = service.start_backtest(["bands", "forecast", "peak"], 2025)
        assert service.get_job(job_id) == {"status": "running", "run_ids": [], "skipped": {}, "error": None}
        assert service.start_backtest(["peak"], 2024) == job_id  # busy: same job
        job = wait_done(job_id)
        assert job == {"status": "done", "run_ids": ["r-bands", "r-peak", "r-ceiling"],
                       "skipped": {"forecast": "not built"}, "error": None}
        assert done_event.wait(5) and seen[-1] == (job_id, job)
        again = service.start_backtest(["peak"], 2025)
        assert again != job_id
        wait_done(again)
    finally:
        service._listeners.clear()


def test_failed_job_reports_a_plain_sentence(monkeypatch):
    message = "There are no real ERCOT prices for 2019 (LZ_HOUSTON, LZ_NORTH) to replay."
    monkeypatch.setattr(service, "runner_command", fake_runner(0.0, {"run_ids": [], "error": message}, 2))
    job = wait_done(service.start_backtest(["bands"], 2019))
    assert job == {"status": "failed", "run_ids": [], "skipped": {}, "error": message}
    monkeypatch.setattr(service, "runner_command", lambda *a: [sys.executable, "-c", "import sys; sys.exit(1)"])
    job = wait_done(service.start_backtest(["bands"], 2025))
    assert job["status"] == "failed" and job["error"] == "The full-year run failed."


def test_unknown_job_raises_key_error():
    with pytest.raises(KeyError):
        service.get_job("no-such-job")


# --- the CLI ---------------------------------------------------------------------------------


def test_cli_full_year_small_fleet(tmp_path, capsys):
    status_file = tmp_path / "status.json"
    code = runner.main(["--strategies", "peak,forecast", "--year", "2025", "--fleet-size", "4", "--no-ceiling",
                        "--no-lag0", "--no-db", "--out", str(tmp_path), "--status-file", str(status_file)])
    assert code == 0
    status = json.loads(status_file.read_text())
    assert len(status["run_ids"]) == 1 and "forecast" in status["skipped"] and status["error"] is None
    out = capsys.readouterr().out
    assert "Evening peak (peak), 2025" in out and "SKIPPED" in out
    record = json.loads((tmp_path / "runs" / f"{status['run_ids'][0]}.json").read_text())
    assert record["metrics"]["coverage"]["fraction"] == 1.0  # the whole of 2025 has prices
    assert record["metrics"]["ticks_replayed"] == 105120
    assert not (tmp_path / "pitch_2025.json").exists()  # not a pitch run


def test_cli_records_out_stores_nothing(tmp_path, capsys):
    """The API's jobs run the CLI with --records-out: it computes, writes the records there and stores nothing."""
    status_file, records_file, out = tmp_path / "status.json", tmp_path / "records.json", tmp_path / "out"
    code = runner.main(["--strategies", "peak", "--year", "2025", "--fleet-size", "4", "--no-ceiling", "--no-lag0",
                        "--pitch", "--out", str(out), "--status-file", str(status_file), "--records-out",
                        str(records_file)])
    assert code == 0
    records = json.loads(records_file.read_text())
    assert [r["strategy"] for r in records] == ["peak"] and records[0]["metrics"]["insights"]["spike_events"] == 21
    assert json.loads(status_file.read_text())["run_ids"] == [records[0]["run_id"]]
    assert not out.exists()  # no JSON export, no pitch set: the job stores them if it isn't cancelled
    assert "stored: nothing" in capsys.readouterr().out


def test_cli_year_without_prices(tmp_path):
    status_file = tmp_path / "status.json"
    assert runner.main(["--strategies", "peak", "--year", "2019", "--no-db", "--out", str(tmp_path),
                        "--status-file", str(status_file)]) == 2
    assert "no real ERCOT prices for 2019" in json.loads(status_file.read_text())["error"]


def test_the_forecast_coverage_guard_checks_every_city():
    """One headline number is one decision method for the whole fleet: 'forecast' runs only when every city's
    forecasts cover the year (here: the mini feed's three weeks). The forecast books are synthetic test doubles."""
    import numpy as np

    from src.backtest.runner import ForecastsMissing, build_context, run_strategy
    from src.forecast.provider import ForecastBook
    from src.strategy.forecast import ForecastStrategy

    ctx = build_context(2025, settings=mini_settings(), fleet_size=20, feed=mini_feed())
    first = int(ctx.series[LOCS[0]].replayed[0].ts.timestamp()) - 3600
    issued = (first + np.arange(22 * 24 + 2, dtype=np.int64) * 3600) * 10**9  # hourly, over all 21 days
    shape = np.tile(np.linspace(20.0, 80.0, 96, dtype=np.float32), (len(issued), 1))

    def books(locations):
        return {loc: ForecastBook(loc, "rt_price", "synthetic", 15, issued, issued.copy(), shape - 5, shape,
                                  shape + 5) for loc in locations}

    some = books(LOCS[:2])
    with pytest.raises(ForecastsMissing) as err:
        run_strategy("forecast", ctx, strategies={loc: ForecastStrategy(some, duration_h=2.0) for loc in LOCS})
    assert err.value.locations == list(LOCS[2:]) and "forecasts cover 0.0% at LZ_CPS, 0.0% at LZ_AEN" in str(err.value)
    assert err.value.plain == "its AI price forecasts for San Antonio and Austin aren't ready for 2025 yet."
    every = books(LOCS)
    record = run_strategy("forecast", ctx, strategies={loc: ForecastStrategy(every, duration_h=2.0) for loc in LOCS})
    assert list(record["per_location"]) == list(LOCS) and record["metrics"]["sold_kwh"] > 0
