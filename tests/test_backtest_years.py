"""Results follow the chosen year (docs/API.md "Results", POST /backtest {"year": 2026}, ?year=).

2026 is replayed as the live fleet replays it: real ERCOT prices up to real_until_utc (the committed snapshot: real
until 2026-09-20 05:00Z), then the AI's stored prediction (p50). Every strategy and the ceiling split their money at
the cutoff into real and projected, and the two add up. The predictions and the forecasts below are SYNTHETIC test
doubles written where the prediction job and the forecast job write theirs (no TimesFM here), so every number can be
checked exactly. A small fleet (20 batteries) over three real weeks and one predicted week keeps it quick.
No database here (tests/test_backtest_store_pg.py covers Postgres).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.backtest import runner, service, store
from src.backtest.engine import local_midnight_utc
from src.backtest.results import build_ai_gains, build_results
from src.backtest.runner import NoPriceData, backtest_settings, build_context, run_backtest, run_strategy
from src.config import load_config
from src.data import years
from src.feeds.memory import InMemoryPriceFeed
from src.feeds.parquet import load_rt_rows
from src.forecast import predictions
from src.forecast import store as fstore

UTC = timezone.utc
LOCS = tuple(load_config().fleet.locations)
CUTOFF = pd.Timestamp("2026-09-20T05:00Z")  # the committed 2026 snapshot's real_until_utc (midnight CDT)
FIRST = "2026-08-30"  # three real weeks before the cutoff (the bands window warms up on the first week)
PREDICTED_DAYS = 7


def synthetic_path(loc: str, k: int, days: int = PREDICTED_DAYS) -> predictions.PredictionPath:
    """SYNTHETIC p50: a daily wave, 20-80 $/MWh, peaking in the evening; each city offset by k dollars."""
    n = days * 96
    ts = CUTOFF.value + np.arange(n, dtype=np.int64) * 15 * 60 * 10**9
    hour = pd.to_datetime(ts, utc=True).tz_convert("America/Chicago").hour.to_numpy() + 0.0
    p50 = np.round(50.0 + k + 30.0 * np.sin((hour - 13.0) / 24.0 * 2 * np.pi), 2)
    return predictions.PredictionPath(loc, "timesfm-3.0", CUTOFF, pd.Timestamp("2026-09-26T12:00Z"), ts,
                                      np.round(p50 - 8, 2), p50, np.round(p50 + 8, 2), np.zeros(n, dtype=np.int16))


def real_feed(first: str = FIRST) -> InMemoryPriceFeed:
    """The committed 2026 snapshot's real prices from `first` on (the whole real part of the window)."""
    start = local_midnight_utc(date.fromisoformat(first))
    rows = [r for r in load_rt_rows(years=(2026,), locations=LOCS) if r.ts >= start]
    return InMemoryPriceFeed.from_intervals(rows)


def mini_settings() -> dict:
    s = backtest_settings()
    s["ceiling"].update(ratio_step=0.25, workers=1)
    return s


def synthetic_real_forecasts(folder) -> None:
    """SYNTHETIC rolling forecasts (test doubles, not TimesFM) issued hourly before the cutoff, in the parquet store
    where src.forecast.job writes them: 24 h of 15-min points on a fixed daily shape."""
    issued = pd.date_range(pd.Timestamp(FIRST, tz="America/Chicago").tz_convert("UTC") - pd.Timedelta(hours=1),
                           CUTOFF, freq="60min", inclusive="left")
    rows = []
    for loc in LOCS:
        for t in issued:
            hours = (t.tz_convert("America/Chicago").hour + np.arange(96) / 4.0) % 24
            p50 = (45.0 + 25.0 * np.sin((hours - 13.0) / 24.0 * 2 * np.pi)).astype(np.float32)
            rows.append(fstore.ForecastRow(t, loc, "rt_price", "timesfm-3.0", t, 15, p50 - 5, p50, p50 + 5))
    fstore.ParquetSink(folder).write(rows)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Snapshot prices (whatever DATABASE_URL says), the synthetic prediction and forecasts, no database."""
    from src.forecast import settings as fsettings

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("PRICE_SOURCE", raising=False)
    monkeypatch.setenv("SERIES_BACKEND", "snapshot")
    monkeypatch.setenv("FORECAST_SOURCE", "parquet")
    monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setattr(predictions, "default_cache_dir", lambda: tmp_path / "predictions")
    real_settings = fsettings.load_settings
    monkeypatch.setattr(fsettings, "load_settings",
                        lambda *a, **kw: real_settings(*a, **kw).with_overrides(cache_dir=tmp_path / "forecasts"))
    years.clear_cache()
    predictions.clear_cache()
    predictions.write([synthetic_path(loc, k) for k, loc in enumerate(LOCS)], out="parquet",
                      cache_dir=tmp_path / "predictions")
    synthetic_real_forecasts(tmp_path / "forecasts")
    yield tmp_path
    years.clear_cache()
    predictions.clear_cache()


def test_2026_replays_real_prices_then_the_prediction(world):
    ctx = build_context(2026, settings=mini_settings(), fleet_size=20, feed=real_feed())
    assert ctx.real_until == CUTOFF.to_pydatetime() and ctx.real_until_utc == "2026-09-20T05:00:00Z"
    assert ctx.prediction["locations"] == list(LOCS) and ctx.prediction["model"] == "timesfm-3.0"
    assert ctx.prediction["cutoff_utc"] == "2026-09-20T05:00:00Z"
    real = {loc: pd.Series({r.ts: r.price_usd_mwh for r in load_rt_rows(years=(2026,), locations=(loc,))})
            for loc in LOCS}
    for k, loc in enumerate(LOCS):
        ticks = ctx.series[loc].replayed
        before = [t for t in ticks if t.ts < CUTOFF]
        after = [t for t in ticks if t.ts >= CUTOFF]
        assert before and after and len(after) == PREDICTED_DAYS * 288  # to the end of the stored prediction
        for t in before[::97]:  # real ERCOT: the interval's price, held for its three ticks
            assert t.price_usd_mwh == pytest.approx(real[loc][pd.Timestamp(t.ts).floor("15min")])
        path = synthetic_path(loc, k)
        for t in after[::53]:  # the prediction's p50, held for its three ticks
            i = int((pd.Timestamp(t.ts).floor("15min") - CUTOFF) / pd.Timedelta(minutes=15))
            assert t.price_usd_mwh == path.p50[i]
    params = ctx.params()
    assert params["real_until_utc"] == "2026-09-20T05:00:00Z"
    assert params["prices"]["after_real_until"]["model"] == "timesfm-3.0"
    # a fully real year has no cutoff and nothing predicted
    assert runner.real_until_of(2025, LOCS, "snapshot") is None
    assert runner.real_until_of(2026, LOCS, "snapshot") == CUTOFF.to_pydatetime()


def test_without_a_prediction_the_full_year_cannot_be_replayed(world, tmp_path, monkeypatch):
    monkeypatch.setattr(predictions, "default_cache_dir", lambda: tmp_path / "nothing")
    predictions.clear_cache()
    with pytest.raises(NoPriceData) as err:
        build_context(2026, settings=mini_settings(), fleet_size=20, feed=real_feed())
    assert str(err.value) == ("Real 2026 prices end on Sep 19, and the AI's prediction for the rest of the year isn't "
                              "ready yet for Houston, Dallas–Fort Worth, San Antonio and Austin, so the full year "
                              "can't be replayed.")


@pytest.fixture
def run2026(world):
    """The 2026 pitch set of a small fleet across the cutoff: bands, peak, the AI (on the synthetic real forecasts
    before the cutoff and the virtual ones after it) and the ceiling, stored in the JSON export."""
    outcome = run_backtest(["bands", "forecast", "peak"], 2026, settings=mini_settings(), pitch=True, use_db=False,
                           fleet_size=20, feed=real_feed(), log=lambda s: None)
    return world, outcome


def test_the_money_splits_at_the_cutoff_and_adds_up(run2026):
    _, outcome = run2026
    assert not outcome.skipped  # the AI ran: real forecasts before the cutoff, virtual ones after it
    by_name = {r["strategy"]: r for r in outcome.records}
    for name in ("bands", "forecast", "peak"):
        r = by_name[name]
        m, split = r["metrics"], r["metrics"]["split"]
        assert split["real_until_utc"] == "2026-09-20T05:00:00Z"
        # the two halves add up to the Ledger's totals, per battery, for the fleet and per city
        assert split["real_per_battery_usd"] + split["projected_per_battery_usd"] == pytest.approx(
            m["per_battery_usd_year"], abs=1e-9)
        assert split["real_usd"] + split["projected_usd"] == pytest.approx(r["fleet_total_usd"], abs=1e-9)
        for loc, per in split["by_location"].items():
            assert per["real_usd"] + per["projected_usd"] == pytest.approx(r["per_location"][loc], abs=1e-9)
        assert split["ticks_projected"] == PREDICTED_DAYS * 288 and split["ticks_real"] > 0
        # recomputed independently: each tick's energy at its price, before and after the cutoff
        assert split["projected_usd"] != 0.0 and split["real_usd"] != 0.0
        assert r["params"]["real_until_utc"] == "2026-09-20T05:00:00Z"
        assert "real until 2026-09-20T05:00:00Z" in r["notes"]
    ceiling = by_name["hindsight"]["metrics"]
    split = ceiling["split"]
    assert split["real_until_utc"] == "2026-09-20T05:00:00Z"
    assert split["real_per_battery_usd"] + split["projected_per_battery_usd"] == pytest.approx(
        ceiling["per_battery_usd_year"], abs=1e-9)
    assert 0 < split["projected_per_battery_usd"] < split["real_per_battery_usd"]  # one predicted week of four
    assert split["capped"]["real_per_battery_usd"] + split["capped"]["projected_per_battery_usd"] == pytest.approx(
        ceiling["capped"]["per_battery_usd_year"], abs=1e-9)
    # hindsight beats every strategy over the window (not necessarily on each side: a strategy can carry charge
    # bought before the cutoff into the predicted week, while the per-day plan ends each day where it started)
    for name in ("bands", "forecast", "peak"):
        assert ceiling["per_battery_usd_year"] > by_name[name]["metrics"]["per_battery_usd_year"]


def test_the_ai_acts_on_real_forecasts_then_on_virtual_ones(world):
    """As the live coordinator: make_forecast_strategy's books hold the real rolling forecasts before the cutoff and
    hourly slices of the stored prediction after it (read against the backtest's own price source)."""
    ctx = build_context(2026, settings=mini_settings(), fleet_size=20, feed=real_feed())
    strategy = runner.make_named_strategy("forecast", ctx.cfg, "LZ_NORTH", price_source=ctx.price_source)
    book = strategy.books["LZ_NORTH"]
    issued = pd.to_datetime(book.issued, utc=True)
    assert issued[0] < CUTOFF and (issued < CUTOFF).sum() > 400  # three weeks of hourly real forecasts
    after = np.flatnonzero(issued >= CUTOFF)
    assert issued[after[0]] == CUTOFF and (np.diff(book.issued[after]) == 3600 * 10**9).all()
    path = synthetic_path("LZ_NORTH", 1)
    i = after[5]  # issued 5 h after the cutoff: the prediction's next 24 h
    assert np.array_equal(book.p50[i], path.p50[20:116].astype(np.float32))
    record = run_strategy("forecast", ctx, lag0=False)
    assert record["metrics"]["split"]["projected_usd"] > 0  # it trades the predicted wave


def test_results_and_ai_gains_follow_the_chosen_year(run2026, tmp_path):
    world, outcome = run2026
    # 2025 has its own pitch set, which a 2026 run leaves alone
    feed25 = InMemoryPriceFeed.from_intervals([r for r in load_rt_rows(years=(2025,), locations=LOCS)
                                               if local_midnight_utc(date(2025, 6, 1)) - timedelta(days=7) <= r.ts
                                               < local_midnight_utc(date(2025, 6, 22))])
    out25 = run_backtest(["bands", "peak"], 2025, settings=mini_settings(), pitch=True, use_db=False, fleet_size=20,
                         feed=feed25, log=lambda s: None)
    before25 = json.loads((world / "results" / "pitch_2025.json").read_text())
    again = run_backtest(["bands"], 2026, settings=mini_settings(), pitch=True, use_db=False, fleet_size=20,
                         feed=real_feed(), ceiling=False, log=lambda s: None)
    assert json.loads((world / "results" / "pitch_2025.json").read_text()) == before25  # untouched
    pitch26 = {r["strategy"]: r["run_id"] for r in store.load_pitch_records_json(2026)}
    assert pitch26["bands"] == again.records[0]["run_id"]  # the newer 2026 bands run replaced only its own row
    assert pitch26["peak"] == next(r["run_id"] for r in outcome.records if r["strategy"] == "peak")

    results = service.pitch_results(2026)
    assert results["year"] == 2026 and results["real_until_utc"] == "2026-09-20T05:00:00Z"
    for s in results["strategies"] + [results["ceiling"]]:
        # to the cent: projected = the rounded total minus the rounded real part
        assert round(s["real_per_battery_usd"] + s["projected_per_battery_usd"], 2) == s["per_battery_usd_year"]
        assert s["projected_per_battery_usd"] > 0
    assert next(s for s in results["strategies"] if s["is_headline"])["strategy"] == "forecast"
    text = " ".join(results["assumptions"])
    assert "for every 5-minute step of 2026 up to Sep 19, 2026." in text
    assert "ERCOT's real prices end on Sep 19, 2026. After that, the prices are the AI's prediction (TimesFM-3" in text
    assert "trades on hourly slices of that same prediction" in text
    # a fully real year: real_until_utc null, everything real
    r25 = service.pitch_results(2025)
    assert r25["year"] == 2025 and r25["real_until_utc"] is None
    assert all(s["projected_per_battery_usd"] == 0.0 and s["real_per_battery_usd"] == s["per_battery_usd_year"]
               for s in r25["strategies"] + [r25["ceiling"]])
    assert service.pitch_results() == r25  # the default is the pitch year

    gains26 = service.ai_gains(2026)
    first = gains26["years"][0]
    assert (first["year"], first["is_pitch"], first["real_until_utc"]) == (2026, True, "2026-09-20T05:00:00Z")
    ai, bands = first["strategies"]["forecast"], first["strategies"]["bands"]
    for s in first["strategies"].values():  # totals (real + projected), and the split
        assert round(s["real_per_battery_usd"] + s["projected_per_battery_usd"], 2) == s["per_battery_usd_year"]
    by_name = {r["strategy"]: r for r in outcome.records}
    assert ai["per_battery_usd_year"] == round(by_name["forecast"]["per_battery_avg_usd"], 2)
    up = first["uplift"]
    assert up["per_battery_usd_year"] == round(ai["per_battery_usd_year"] - bands["per_battery_usd_year"], 2)
    assert round(up["real_per_battery_usd"] + up["projected_per_battery_usd"], 2) == up["per_battery_usd_year"]
    assert up["real_per_battery_usd"] == round(ai["real_per_battery_usd"] - bands["real_per_battery_usd"], 2)
    # the default (the pitch year) lists 2025 first; 2025 has no AI run here, so no uplift
    gains = service.ai_gains()
    assert gains["years"][0]["year"] == 2025 and gains["years"][0]["is_pitch"] is True
    assert gains["years"][0]["real_until_utc"] is None and "uplift" not in gains["years"][0]
    assert [y["year"] for y in gains["years"]] == [2025, 2026] and gains["years"][1]["is_pitch"] is False
    assert out25.records and gains["years"][1]["real_until_utc"] == "2026-09-20T05:00:00Z"


def test_the_api_serves_results_by_year(run2026, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api import main

    world, _ = run2026
    main.configure(url="redis://127.0.0.1:1/0", prefix="test:years:")
    try:
        with TestClient(main.app) as client:
            r = client.get("/results/pitch", params={"year": 2026})
            assert r.status_code == 200 and r.json() == json.loads(json.dumps(service.pitch_results(2026)))
            assert r.json()["real_until_utc"] == "2026-09-20T05:00:00Z"
            r = client.get("/results/ai-gains?year=2026")
            assert r.status_code == 200 and r.json()["years"][0]["year"] == 2026
            r = client.get("/results/pitch")  # the pitch year (2025) has no results in this folder
            assert r.status_code == 404 and r.json() == {"error": "no_results", "message": "No full-year results yet."}
            assert client.get("/results/pitch?year=2019").status_code == 404
            for path in ("/results/pitch?year=next", "/results/ai-gains?year=2026.5"):
                r = client.get(path)
                assert r.status_code == 400 and r.json()["error"] == "invalid_year", path
            # the default (2025) has no pitch set here, so other years are compared on config.yaml's 20,000-battery
            # fleet, and this small 2026 fleet doesn't qualify: nothing to show
            r = client.get("/results/ai-gains")
            assert r.status_code == 404 and r.json()["error"] == "no_results"
    finally:
        main.state.backend = None


def test_ai_gains_rules_by_year():
    """SYNTHETIC slim records: the requested year's pitch set first; another year's own complete pitch set is
    preferred over newer unflagged runs; is_pitch marks only the requested year's set."""
    fleet = {"size": 20000, "by_location": {"LZ_HOUSTON": 7000, "LZ_NORTH": 8000, "LZ_CPS": 2600, "LZ_AEN": 2400}}

    def rec(strategy, year, pb, *, pitch=False, finished="2026-09-26T10:00:00Z", split=None):
        return {"run_id": f"{strategy}-{year}-{finished}", "strategy": strategy, "year": year, "label": strategy,
                "fleet": fleet, "fleet_total_usd": pb * 20000, "per_battery_avg_usd": pb, "insights": None,
                "split": split, "is_pitch": pitch, "started_at": finished, "finished_at": finished}

    cut = {"real_until_utc": "2026-09-20T05:00:00Z"}
    records = [rec("forecast", 2025, 190.95, pitch=True), rec("bands", 2025, 123.67, pitch=True),
               rec("peak", 2025, 136.70, pitch=True), rec("hindsight", 2025, 421.29, pitch=True),
               rec("bands", 2025, 99.0, finished="2026-09-28T10:00:00Z"),  # newer, unflagged: not the 2025 set
               rec("forecast", 2026, 180.0, pitch=True, split={**cut, "real_per_battery_usd": 140.004}),
               rec("bands", 2026, 110.0, pitch=True, split={**cut, "real_per_battery_usd": 90.0}),
               rec("peak", 2026, 120.0, pitch=True, split={**cut, "real_per_battery_usd": 95.0}),
               rec("hindsight", 2026, 400.0, pitch=True, split={**cut, "real_per_battery_usd": 300.0})]
    gains = build_ai_gains(records, pitch_year=2025, year=2026, settings=backtest_settings())
    y26, y25 = gains["years"]
    assert (y26["year"], y26["is_pitch"], y25["year"], y25["is_pitch"]) == (2026, True, 2025, False)
    assert y26["real_until_utc"] == "2026-09-20T05:00:00Z" and y25["real_until_utc"] is None
    assert y26["strategies"]["forecast"]["real_per_battery_usd"] == 140.0
    assert y26["strategies"]["forecast"]["projected_per_battery_usd"] == 40.0
    assert y26["uplift"]["per_battery_usd_year"] == 70.0
    assert (y26["uplift"]["real_per_battery_usd"], y26["uplift"]["projected_per_battery_usd"]) == (50.0, 20.0)
    assert y26["best_possible_per_battery_usd_year"] == 400.0
    assert y25["strategies"]["bands"]["per_battery_usd_year"] == 123.67  # its pitch set, not the newer run
    assert y25["strategies"]["bands"]["projected_per_battery_usd"] == 0.0
    assert y25["uplift"]["projected_per_battery_usd"] == 0.0
    # the default request is the pitch year: 2025 first, 2026 after it
    default = build_ai_gains(records, pitch_year=2025, settings=backtest_settings())
    assert [(y["year"], y["is_pitch"]) for y in default["years"]] == [(2025, True), (2026, False)]


# --- POST /backtest {"year": 2026}: the job and its cancel ----------------------------------------------------------


def stand_in(records_json: str, status: dict, delay_s: float):
    """Stands in for the runner subprocess (a real full-year run takes a minute): after `delay_s` it writes the
    records and the status file the real runner would write."""

    def command(names, year, status_file, records_file=None):
        code = (f"import sys, time; time.sleep({delay_s}); "
                f"open({records_file!r}, 'w').write({records_json!r}); "
                f"open({status_file!r}, 'w').write({json.dumps(status)!r})")
        return [sys.executable, "-c", code]

    return command


def _wait(job_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if service.get_job(job_id)["status"] != "running":
            return service.get_job(job_id)
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_post_backtest_2026_runs_and_cancels_without_touching_2025(run2026, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api import main

    world, outcome = run2026
    records = [r for r in outcome.records]
    status = {"run_ids": [r["run_id"] for r in records if r["strategy"] != "hindsight"], "skipped": {},
              "error": None, "ceiling_run_id": outcome.ceiling["run_id"]}
    # a 2025 pitch set is already there
    fake25 = [dict(r, run_id=f"{r['run_id']}-25", year=2025, params={**r["params"], "year": 2025,
                                                                     "real_until_utc": None},
                   metrics={k: v for k, v in r["metrics"].items() if k != "split"}) for r in records]
    store.save_records(fake25, pitch=True, use_db=False)
    before25 = (world / "results" / "pitch_2025_runs.json").read_text()
    (world / "results" / "pitch_2026_runs.json").unlink()
    (world / "results" / "pitch_2026.json").unlink()
    main.configure(url="redis://127.0.0.1:1/0", prefix="test:years:")
    events = []
    monkeypatch.setattr(main.backend(), "emit", lambda kind, severity, message, **kw: events.append((kind, message)))
    monkeypatch.setattr(service, "CANCEL_GRACE_S", 0.5)
    seen_years = []
    command = stand_in(json.dumps(store.clean(records)), status, 20.0)
    monkeypatch.setattr(service, "runner_command", lambda n, y, s, r=None: (seen_years.append(y), command(n, y, s, r))[1])
    try:
        with TestClient(main.app) as client:
            r = client.post("/backtest", json={"strategies": ["bands", "forecast", "peak"], "year": 2026})
            assert r.status_code == 202
            job_id = r.json()["job_id"]
            deadline = time.time() + 10
            while not service._jobs[job_id]["pid"] and time.time() < deadline:
                time.sleep(0.02)
            assert seen_years == [2026]  # the runner is asked for 2026
            r = client.post(f"/backtest/{job_id}/cancel")
            assert r.status_code == 200 and r.json() == {"status": "cancelled"}
            assert service._jobs[job_id]["settled"].wait(10)
            assert client.get(f"/backtest/{job_id}").json()["status"] == "cancelled"
            assert not (world / "results" / "pitch_2026_runs.json").exists()  # nothing stored
            assert client.get("/results/pitch?year=2026").status_code == 404
            assert ("backtest_done", "The full-year 2026 run was stopped before it finished, so there are no "
                                     "full-year results for 2026 yet.") in events
            # run it for real (the stand-in finishes at once): stored as 2026's pitch set
            monkeypatch.setattr(service, "runner_command", stand_in(json.dumps(store.clean(records)), status, 0.0))
            job_id = client.post("/backtest", json={"strategies": ["bands", "forecast", "peak"], "year": 2026}
                                 ).json()["job_id"]
            assert _wait(job_id)["status"] == "done"
            r = client.get("/results/pitch?year=2026")
            assert r.status_code == 200 and r.json()["real_until_utc"] == "2026-09-20T05:00:00Z"
            assert (world / "results" / "pitch_2025_runs.json").read_text() == before25  # 2025 untouched
            assert client.get("/results/pitch").json()["year"] == 2025
    finally:
        for job_id, job in list(service._jobs.items()):
            if job["status"] == "running" and job["proc"] is not None:
                service._stop(job["proc"], grace_s=0.0)
        main.state.backend = None
