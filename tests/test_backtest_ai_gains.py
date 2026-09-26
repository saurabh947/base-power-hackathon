"""GET /results/ai-gains (docs/API.md AiGains / StrategyGains): built by src.backtest.service.ai_gains from stored runs.

- The builder's rules on hand-made slim records (SYNTHETIC numbers: the rules, not results).
- The real pipeline: mini backtests (20 batteries, four weeks of real 2025 and 2024 prices from the snapshot) stored
  in the JSON export, read back by the service and served by the API. There are no TimesFM forecasts in a local
  checkout, so the forecast runs here use SYNTHETIC forecast books (test doubles, labelled as such).
Postgres: tests/test_backtest_store_pg.py.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

import numpy as np
import pytest

from src.backtest import service, store
from src.backtest.engine import local_midnight_utc
from src.backtest.results import build_ai_gains, config_fleet_signature
from src.backtest.runner import backtest_settings, build_context, run_backtest, run_strategy
from src.config import REPO_ROOT, load_config
from src.feeds.memory import InMemoryPriceFeed
from src.feeds.parquet import load_rt_rows

LOCS = tuple(load_config().fleet.locations)
INSIGHTS = {"surplus_mwh", "spike_mwh", "spike_events", "spike_charge_level", "sold_mwh", "bought_mwh",
            "avg_sell_usd_mwh", "avg_buy_usd_mwh"}


def contract() -> tuple[dict, dict]:
    """(AiGains example with StrategyGains placeholders filled, StrategyGains example) from docs/API.md."""
    text = (REPO_ROOT / "docs" / "API.md").read_text()
    gains = re.search(r"\*\*AiGains\*\* \(`GET /results/ai-gains`\)\s*```json\s*(\{.*?\})\s*```", text, re.S).group(1)
    strategy = re.search(r"StrategyGains is:\s*```json\s*(\{.*?\})\s*```", text, re.S).group(1)
    return json.loads(gains.replace("StrategyGains", "{}")), json.loads(strategy)


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)


# --- the rules, on hand-made slim records (SYNTHETIC numbers) --------------------------------------------------

FLEET = {"size": 20000, "by_location": {"LZ_HOUSTON": 7000, "LZ_NORTH": 8000, "LZ_CPS": 2600, "LZ_AEN": 2400}}
OTHER_FLEET = {"size": 20000, "by_location": {"LZ_HOUSTON": 10000, "LZ_NORTH": 10000}}


def rec(strategy, year, per_battery, *, pitch=False, finished="2026-09-26T10:00:00Z", fleet=FLEET, insights=True):
    """A SYNTHETIC slim record."""
    ins = None
    if insights:
        ins = {"revenue_usd": per_battery * fleet["size"], "sold_mwh": 80168.14, "bought_mwh": 89231.47,
               "avg_sell_usd_mwh": 66.581, "avg_buy_usd_mwh": 17.019, "surplus_mwh": 7011.33, "spike_mwh": 25.21,
               "spike_events": 21, "spike_charge_level": 0.23147, "spike_intervals_5min": 108, "spike_usd_mwh": 1000.0}
    return {"run_id": f"{strategy}-{year}-{finished}", "strategy": strategy, "year": year, "label": strategy,
            "fleet": fleet, "fleet_total_usd": per_battery * fleet["size"], "per_battery_avg_usd": per_battery,
            "insights": ins if strategy != "hindsight" else None, "is_pitch": pitch, "started_at": finished,
            "finished_at": finished}


def test_the_rules():
    records = [
        # the pitch set (2025), plus an older flagged forecast run that the newest one replaces
        rec("forecast", 2025, 190.95, pitch=True), rec("bands", 2025, 123.67, pitch=True),
        rec("peak", 2025, 136.70, pitch=True), rec("hindsight", 2025, 421.29, pitch=True),
        rec("forecast", 2025, 150.0, pitch=True, finished="2026-09-20T10:00:00Z"),
        rec("bands", 2025, 99.0, finished="2026-09-27T10:00:00Z"),  # newer but not in the pitch set: ignored
        # 2024: the newest complete set on the pitch set's fleet; old two-city runs and a newer bands run
        rec("forecast", 2024, 271.45, insights=False), rec("bands", 2024, 113.71, insights=False),
        rec("peak", 2024, 150.51, insights=False), rec("hindsight", 2024, 526.54),
        rec("bands", 2024, 113.13, finished="2026-09-25T10:00:00Z", fleet=OTHER_FLEET),
        rec("forecast", 2024, 265.01, finished="2026-09-27T12:00:00Z", fleet=OTHER_FLEET),
        # 2023: incomplete (no peak run): left out
        rec("forecast", 2023, 200.0), rec("bands", 2023, 100.0),
    ]
    gains = build_ai_gains(records, pitch_year=2025, settings=backtest_settings())
    assert (gains["ai"], gains["baseline"]) == ("forecast", "bands")
    assert [y["year"] for y in gains["years"]] == [2025, 2024] and [y["is_pitch"] for y in gains["years"]] == [True,
                                                                                                             False]
    y25, y24 = gains["years"]
    assert list(y25) == ["year", "is_pitch", "best_possible_per_battery_usd_year", "strategies", "uplift"]
    assert list(y25["strategies"]) == ["forecast", "bands", "peak"]
    ai = y25["strategies"]["forecast"]
    assert ai["label"] == "AI forecast (TimesFM-3)" and y25["strategies"]["bands"]["label"] == "Simple rule"
    assert ai["per_battery_usd_year"] == 190.95 and ai["fleet_usd_year"] == 3819000.0
    assert ai["share_of_best"] == round(190.95 / 421.29, 3) == 0.453
    assert (ai["surplus_mwh"], ai["spike_mwh"], ai["spike_events"], ai["spike_charge_level"]) == (7011.3, 25.2, 21,
                                                                                                    0.231)
    assert (ai["sold_mwh"], ai["bought_mwh"], ai["avg_sell_usd_mwh"], ai["avg_buy_usd_mwh"]) == (80168.1, 89231.5,
                                                                                                  66.58, 17.02)
    assert y25["uplift"] == {"per_battery_usd_year": 67.28, "pct": round(67.28 / 123.67, 3),
                             "fleet_usd_year": round(67.28 * 20000, 2)}
    assert y25["best_possible_per_battery_usd_year"] == 421.29
    # 2024: the four-city set, whose runs predate metrics.insights: money only, no insight fields
    assert y24["strategies"]["forecast"]["per_battery_usd_year"] == 271.45
    assert y24["strategies"]["bands"]["per_battery_usd_year"] == 113.71
    for s in y24["strategies"].values():
        assert set(s) == {"label", "per_battery_usd_year", "fleet_usd_year", "share_of_best"}
    assert y24["uplift"]["pct"] == round((271.45 - 113.71) / 113.71, 3)
    # no hindsight run for a year: no best possible, no share_of_best
    no_best = build_ai_gains([r for r in records if not (r["strategy"] == "hindsight" and r["year"] == 2025)],
                             pitch_year=2025, settings=backtest_settings())
    assert "best_possible_per_battery_usd_year" not in no_best["years"][0]
    assert all("share_of_best" not in s for s in no_best["years"][0]["strategies"].values())


def test_a_pitch_set_without_the_ai_still_shows_and_nothing_is_empty():
    records = [rec("bands", 2025, 123.67, pitch=True), rec("peak", 2025, 136.70, pitch=True),
               rec("hindsight", 2025, 421.29, pitch=True)]
    gains = build_ai_gains(records, pitch_year=2025, settings=backtest_settings())
    (y25,) = gains["years"]
    assert list(y25["strategies"]) == ["bands", "peak"] and "uplift" not in y25  # nothing to compare the AI with
    assert build_ai_gains([rec("peak", 2025, 1.0, pitch=True)], pitch_year=2025)["years"] == []
    assert build_ai_gains([], pitch_year=2025)["years"] == []
    # other years are compared on the configured fleet when there is no pitch set
    only_2024 = [rec(s, 2024, v) for s, v in (("forecast", 3.0), ("bands", 2.0), ("peak", 1.0))]
    assert [y["year"] for y in build_ai_gains(only_2024, pitch_year=2025,
                                              default_signature=config_fleet_signature())["years"]] == [2024]
    assert build_ai_gains(only_2024, pitch_year=2025, default_signature=(40, (("LZ_HOUSTON", 40),)))["years"] == []


# --- the real pipeline: mini backtests -> JSON export -> service -> API ------------------------------------------


def mini_feed(year: int, first: str, days=21, warmup=7) -> InMemoryPriceFeed:
    start = local_midnight_utc(date.fromisoformat(first))
    rows = [r for r in load_rt_rows(years=(year,), locations=LOCS)
            if start - timedelta(days=warmup) <= r.ts < start + timedelta(days=days)]
    return InMemoryPriceFeed.from_intervals(rows)


def mini_settings():
    s = backtest_settings()
    s["ceiling"].update(ratio_step=0.25, workers=1)
    return s


def synthetic_forecast_run(year: int, feed) -> dict:
    """A forecast run on SYNTHETIC forecast books (test doubles: a fixed daily shape, not TimesFM)."""
    from src.forecast.provider import ForecastBook
    from src.strategy.forecast import ForecastStrategy

    ctx = build_context(year, settings=mini_settings(), fleet_size=20, feed=feed)
    first = int(ctx.series[LOCS[0]].replayed[0].ts.timestamp()) - 3600
    n = len(ctx.series[LOCS[0]].replayed) // 12 + 48
    issued = (first + np.arange(n, dtype=np.int64) * 3600) * 10**9
    shape = np.tile(np.linspace(20.0, 80.0, 96, dtype=np.float32), (n, 1))
    books = {loc: ForecastBook(loc, "rt_price", "synthetic", 15, issued, issued.copy(), shape - 5, shape, shape + 5)
             for loc in LOCS}
    return run_strategy("forecast", ctx, lag0=False,
                        strategies={loc: ForecastStrategy(books, duration_h=2.0) for loc in LOCS})


@pytest.fixture(scope="module")
def stored(tmp_path_factory):
    """2025: the pitch set (bands, peak, ceiling + a synthetic-forecast run). 2024: a non-pitch complete set."""
    out = tmp_path_factory.mktemp("gains")
    runs = {}
    for year, first, pitch in ((2025, "2025-06-01", True), (2024, "2024-08-01", False)):
        feed = mini_feed(year, first)
        outcome = run_backtest(["bands", "peak"], year, settings=mini_settings(), pitch=pitch, use_db=False,
                               fleet_size=20, feed=feed, out_dir=out, log=lambda s: None, fast=True)
        forecast = synthetic_forecast_run(year, feed)
        store.save_records([forecast], pitch=pitch, use_db=False, out_dir=out, settings=mini_settings())
        runs[year] = {r["strategy"]: r for r in outcome.records + [forecast]}
    return out, runs


def test_the_service_serves_the_stored_runs(stored, monkeypatch):
    out, runs = stored
    monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(out))
    gains = service.ai_gains()  # compared on the pitch set's fleet (20 batteries), not config.yaml's 20,000
    example, strategy_example = contract()
    assert set(gains) == set(example) and (gains["ai"], gains["baseline"]) == ("forecast", "bands")
    assert [(y["year"], y["is_pitch"]) for y in gains["years"]] == [(2025, True), (2024, False)]
    for y in gains["years"]:
        assert set(y) == set(example["years"][0]) and list(y["strategies"]) == ["forecast", "bands", "peak"]
        assert set(y["uplift"]) == set(example["years"][0]["uplift"])
        mine = runs[y["year"]]
        assert y["best_possible_per_battery_usd_year"] == round(mine["hindsight"]["per_battery_avg_usd"], 2)
        for name, s in y["strategies"].items():
            assert set(s) == set(strategy_example), name  # every StrategyGains field: these runs have insights
            r = mine[name]
            assert s["per_battery_usd_year"] == round(r["per_battery_avg_usd"], 2)
            assert s["fleet_usd_year"] == round(r["fleet_total_usd"], 2)
            assert s["share_of_best"] == round(r["per_battery_avg_usd"] / mine["hindsight"]["per_battery_avg_usd"], 3)
            ins = r["metrics"]["insights"]
            assert s["surplus_mwh"] == round(ins["surplus_mwh"], 1) and s["spike_events"] == ins["spike_events"]
            assert s["sold_mwh"] == round(r["metrics"]["sold_kwh"] / 1000, 1)
        f, b = mine["forecast"], mine["bands"]
        assert y["uplift"]["per_battery_usd_year"] == round(f["per_battery_avg_usd"] - b["per_battery_avg_usd"], 2)
        assert y["uplift"]["pct"] == round((f["per_battery_avg_usd"] - b["per_battery_avg_usd"])
                                           / b["per_battery_avg_usd"], 3)
    json.dumps(gains, allow_nan=False)


def test_old_runs_without_insights_dont_break_it(stored, monkeypatch, tmp_path):
    import shutil

    out, runs = stored
    copy = tmp_path / "old"
    shutil.copytree(out, copy)
    for path in list((copy / "runs").glob("*.json")) + list(copy.glob("pitch_*_runs.json")):
        data = json.loads(path.read_text())
        for r in data if isinstance(data, list) else [data]:
            (r.get("metrics") or {}).pop("insights", None)  # as stored before metrics.insights existed
        path.write_text(json.dumps(data))
    monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(copy))
    gains = service.ai_gains()
    assert [y["year"] for y in gains["years"]] == [2025, 2024]
    for y in gains["years"]:
        for s in y["strategies"].values():
            assert not INSIGHTS & set(s) and {"per_battery_usd_year", "share_of_best"} <= set(s)


def test_nothing_stored_is_a_404(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(tmp_path))
    with pytest.raises(service.ResultsNotFound):
        service.ai_gains()


def test_the_api_serves_it(stored, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from src.api import main

    out, _ = stored
    main.configure(url="redis://127.0.0.1:1/0", prefix="test:gains:")
    try:
        with TestClient(main.app) as client:
            monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(out))
            r = client.get("/results/ai-gains")
            assert r.status_code == 200 and r.json() == json.loads(json.dumps(service.ai_gains()))
            assert [y["year"] for y in r.json()["years"]] == [2025, 2024]
            monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(tmp_path))  # nothing stored there
            r = client.get("/results/ai-gains")
            assert r.status_code == 404 and r.json() == {"error": "no_results", "message": "No full-year results yet."}
    finally:
        main.state.backend = None
