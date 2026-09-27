"""What the AI adds (src/backtest/insights.py): the insights every record stores are read off the backtest's own
replay, book exactly the pitch backtest's money, and the CLI (including --daily) keeps working.
Real 2025 prices from the committed snapshot, small fleets."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from src.backtest import insights
from src.backtest.engine import TickSeries
from src.backtest.runner import backtest_settings, build_context, run_strategy
from src.core.models import PriceTick

STORED = {"revenue_usd", "sold_mwh", "bought_mwh", "avg_sell_usd_mwh", "avg_buy_usd_mwh", "surplus_mwh", "spike_mwh",
          "spike_events", "spike_charge_level", "spike_intervals_5min", "spike_usd_mwh"}


@pytest.fixture(scope="module", autouse=True)
def snapshot_series():
    """Prices, load and fuel mix from the committed snapshot even when a database is configured."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SERIES_BACKEND", "snapshot")
        yield


@pytest.fixture(scope="module")
def ctx(snapshot_series):
    return build_context(2025, settings=backtest_settings(), fleet_size=40)


def test_every_record_stores_insights_that_book_the_backtests_money(ctx):
    record = run_strategy("peak", ctx, lag0=False)
    m = record["metrics"]
    ins = m["insights"]
    assert set(ins) == STORED
    # the same money as the Ledger (the pitch number), to float rounding: no second opinion on revenue
    assert ins["revenue_usd"] == pytest.approx(record["fleet_total_usd"], rel=1e-12, abs=1e-6)
    assert ins["sold_mwh"] * 1000 == pytest.approx(m["sold_kwh"], rel=1e-12)
    assert ins["bought_mwh"] * 1000 == pytest.approx(m["bought_kwh"], rel=1e-12)
    assert ins["avg_sell_usd_mwh"] == pytest.approx(m["avg_sell_usd_mwh"], rel=1e-9)
    assert ins["avg_buy_usd_mwh"] == pytest.approx(m["avg_buy_usd_mwh"], rel=1e-9)
    # 2025 had real spikes: 21 city spikes >= $1,000/MWh (docs/API.md's example), whoever trades
    assert ins["spike_usd_mwh"] == 1000.0 and ins["spike_events"] == 21 and ins["spike_intervals_5min"] == 108
    assert 0.2 <= ins["spike_charge_level"] <= 1.0 and ins["spike_mwh"] > 0
    assert 0 < ins["surplus_mwh"] < ins["bought_mwh"]
    assert "surplus bought at <= $0" in _report(record)


def _report(record):
    from src.backtest.runner import BacktestOutcome, format_report

    return format_report(BacktestOutcome([record], {}, None, False, []))


def test_the_cli_measures_the_same_numbers(ctx):
    record = run_strategy("bands", ctx, lag0=False)
    got = insights.measure("bands", ctx)  # its own replay, through the same code
    stored = record["metrics"]["insights"]
    assert got["per_battery_usd"] == pytest.approx(record["metrics"]["per_battery_usd_year"], abs=0.005)
    assert got["revenue_usd"] == pytest.approx(record["fleet_total_usd"], abs=0.005)
    assert got["insights_revenue_usd"] == pytest.approx(got["revenue_usd"], abs=0.01)
    for key in ("surplus_mwh", "spike_mwh", "sold_mwh", "bought_mwh"):
        assert got[key] == pytest.approx(stored[key], abs=0.05), key
    assert got["spike_events"] == stored["spike_events"]
    assert got["avg_sell_usd_mwh"] > got["avg_buy_usd_mwh"] > -50
    # the CLI-only grid context: the snapshot holds 2025 load and fuel mix
    assert 0 <= got["charging_renewable_share"] <= 1 and got["peak_hours"] >= 80
    assert got["peak_support_avg_mw"] >= 0


def _synthetic_replay(prices_by_loc, delivered_by_loc, level_by_loc):
    """SYNTHETIC ticks and per-tick fleet energy (hand-checked definitions only)."""
    t0 = datetime(2025, 8, 1, 5, tzinfo=timezone.utc)
    series, runs = {}, {}
    for loc, prices in prices_by_loc.items():
        ticks = [PriceTick(t0 + timedelta(minutes=5 * k), loc, float(p), None, None) for k, p in enumerate(prices)]
        series[loc] = TickSeries(loc, ticks, 0)
        runs[loc] = SimpleNamespace(delivered_kwh=np.array(delivered_by_loc[loc], dtype=float),
                                    fleet_soc=np.array(level_by_loc[loc], dtype=float))
    return SimpleNamespace(runs=runs), series


def test_definitions_by_hand():
    replay, series = _synthetic_replay(
        {"A": [-5, 20, 1200, 1500, 30, 1000, 10], "B": [1100, 50, 50, 2000, 40, 0, 900]},
        {"A": [-100, 0, 50, 60, -10, 70, 0], "B": [30, -20, 0, 80, 0, -40, 0]},
        {"A": [.30, .30, .25, .20, .21, .15, .15], "B": [.80, .82, .82, .70, .70, .72, .72]},
    )
    got = insights.from_replay(replay, series)
    # A: spikes start at steps 2 (1200) and 5 (1000); B: at 0 (no "just before": not counted) and 3 (2000)
    assert got["spike_events"] == 3
    assert got["spike_charge_level"] == pytest.approx((0.30 + 0.21 + 0.82) / 3)
    assert got["spike_intervals_5min"] == 5
    # sold during any spike step, including B's step 0 (a spike, though not a counted start)
    assert got["spike_mwh"] == pytest.approx((50 + 60 + 70 + 30 + 80) / 1000)
    assert got["surplus_mwh"] == pytest.approx((100 + 40) / 1000)  # bought at <= $0: A at -5, B at 0
    assert got["sold_mwh"] == pytest.approx((50 + 60 + 70 + 30 + 80) / 1000)
    assert got["bought_mwh"] == pytest.approx((100 + 10 + 20 + 40) / 1000)
    sell = 50 * 1200 + 60 * 1500 + 70 * 1000 + 30 * 1100 + 80 * 2000
    buy = 100 * -5 + 10 * 30 + 20 * 50 + 40 * 0
    assert got["avg_sell_usd_mwh"] == pytest.approx(sell / 290)
    assert got["avg_buy_usd_mwh"] == pytest.approx(buy / 170)
    assert got["revenue_usd"] == pytest.approx((sell - buy) / 1000)
    none = insights.from_replay(*_synthetic_replay({"A": [10, 20]}, {"A": [0, 0]}, {"A": [.5, .5]}))
    assert none["spike_events"] == 0 and none["spike_charge_level"] is None and none["avg_sell_usd_mwh"] is None


def mini_context(fleet_size=40):
    """Three weeks of real June 2025 prices (after a 7-day warm-up) instead of the whole year: fast."""
    from datetime import date

    from src.backtest.engine import local_midnight_utc
    from src.feeds.memory import InMemoryPriceFeed
    from src.feeds.parquet import load_rt_rows

    start = local_midnight_utc(date(2025, 6, 1))
    rows = [r for r in load_rt_rows(years=(2025,)) if start - timedelta(days=7) <= r.ts < start + timedelta(days=21)]
    return build_context(2025, settings=backtest_settings(), fleet_size=fleet_size,
                         feed=InMemoryPriceFeed.from_intervals(rows))


def test_daily_cli_picks_the_race_days(tmp_path, capsys, monkeypatch):
    """--daily on bands vs peak (the forecasts live in Cloud SQL): per-day revenue adds up to the replay, the top
    days are ranked by peak minus bands, and each is replayed as the live race would run it. The CLI runs end to
    end on three real weeks (`python -m src.backtest.insights --daily` replays the whole year the same way)."""
    import json

    from src.backtest import runner

    small = mini_context()
    monkeypatch.setattr(runner, "build_context", lambda year, settings=None, fleet_size=None: small)
    out = tmp_path / "daily.json"
    assert insights.main(["--year", "2025", "--daily", "--pair", "bands,peak", "--top", "3", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "Top 3 days by peak minus bands, whole day (midnight to midnight, Central time)" in text
    assert "Top 3 days by peak minus bands, evening (6 pm to midnight, Central time)" in text
    assert "both fleets start that day at 90% charge" in text and "/MWh at " in text
    result = json.loads(out.read_text())
    days = result["days"]
    # every local day that had prices (the feed's 7-day warm-up is replayed too: the context spans the year)
    assert list(days)[0] == "2025-05-25" and len(days) == 28
    assert (result["baseline"], result["challenger"]) == ("bands", "peak")
    for name in ("bands", "peak"):  # the days add up to the replay's fleet total (the Ledger's)
        assert sum(d[name] for d in days.values()) == pytest.approx(result["year_total_usd"][name], rel=1e-9)
        assert result["year_total_usd"][name] == pytest.approx(run_strategy(name, small, lag0=False)["fleet_total_usd"],
                                                               rel=1e-12)
    with pytest.raises(SystemExit):
        insights.main(["--daily", "--pair", "bands"])
    for window in ("day", "evening"):
        rows = result["top"][window]
        leads = [r["lead_usd"] for r in rows]
        assert len(rows) == 3 and leads == sorted(leads, reverse=True)
        suffix = "" if window == "day" else "_evening"
        best = max(days, key=lambda d: days[d]["peak" + suffix] - days[d]["bands" + suffix])
        assert rows[0]["date_local"] == best
        for r in rows:
            assert set(r["race"]) == {"bands", "peak"} and r["race_lead_usd"] == pytest.approx(
                r["race"]["peak"] - r["race"]["bands"])
            assert r["race_start_utc"].endswith("Z") and r["top_price"]["usd_mwh"] > 0


def test_race_window_is_the_backtest_from_that_start(ctx):
    """The race check replays [start, end) from the demo charge with a 7-day warm-up: the same as a backtest
    built for that window alone (as tests/test_fleet_backtest_parity.py builds one)."""
    from src.backtest.engine import build_fleets, load_ticks
    from src.backtest.runner import make_named_strategy, replay
    from src.feeds.parquet import snapshot_feed

    cfg = ctx.cfg
    start = datetime(2025, 7, 11, 23, tzinfo=timezone.utc)  # 6 pm CDT, the July 11 spike evening
    end = start + timedelta(hours=6)
    strategies = {name: {loc: make_named_strategy(name, cfg, loc) for loc in ctx.fleets} for name in ("bands", "peak")}
    got = insights.race_window(ctx, strategies, start, end, 0.9)
    series = load_ticks(snapshot_feed(years=(2025,)), list(cfg.fleet.locations), start, end, start - timedelta(days=7))
    fleets = build_fleets(40, cfg.fleet.locations, cfg.battery, cfg.seed, soc_init=0.9)
    for name in ("bands", "peak"):
        want = replay(name, cfg, series, fleets, lag=cfg.sim.decision_lag_ticks, strategies=strategies[name])
        assert got[name] == want.summary["total_usd"]
