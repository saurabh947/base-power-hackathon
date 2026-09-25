"""The vectorized backtest engine reproduces the live path exactly (src/backtest/engine.py).

- Hand-computed tiny cases (1 and 3 batteries, a few ticks) with a SYNTHETIC scripted strategy.
- Bit-identical parity with a command-by-command loop through the seams (InMemoryBatteryAdapter,
  one Command per battery per tick), which is what the worker fleet does.
- Bit-identical equivalence with the M0 skeleton (1 battery, 1 day) for bands and peak, lag 0 and 3.
- Invariants on real prices: SoC in bounds, storage balances with eta, per-tick power limits.
- No look-ahead: shuffling future prices changes no earlier decision.
- The bands fast path equals calling the seam every tick.
Real prices come from the committed ERCOT snapshot (data/snapshot).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import numpy as np
import pytest

from src.backtest.engine import (
    Fleet,
    bands_targets,
    bands_windows,
    build_fleets,
    fill_ledger,
    load_ticks,
    location_counts,
    replay_location,
    uses_bands_fast_path,
    year_bounds,
)
from src.backtest.runner import history_ticks, replay
from src.config import load_config
from src.core.ids import make_command_id
from src.core.models import BatteryState, Command, PriceTick
from src.feeds.memory import InMemoryPriceFeed
from src.feeds.parquet import snapshot_feed
from src.ledger.backtest import BacktestLedger
from src.sim import m0_prices, skeleton
from src.sim.adapters import InMemoryBatteryAdapter
from src.sim.allocation import allocate, fleet_headroom_kw
from src.sim.battery import fleet_soc, grid_kwh_from_soc_change, headroom_kw
from src.sim.decision import decision_tick
from src.sim.fleet import generate_batteries
from src.strategy import make_strategy
from src.strategy.bands import BandsStrategy
from src.strategy.peak import PeakStrategy

UTC = timezone.utc
FIVE = timedelta(minutes=5)
T0 = datetime(2025, 3, 3, 12, 0, tzinfo=UTC)
DT = 5 / 60
CFG = load_config()


class Scripted:
    """SYNTHETIC test strategy: a fixed kW target per decision time, clipped to [-H, H]. Records its inputs."""

    def __init__(self, script: dict[datetime, float]):
        self.script = script
        self.calls = []

    def target_kw(self, tick, fleet_soc, fleet_headroom_kw, price_history):
        self.calls.append((tick, fleet_soc, fleet_headroom_kw, list(price_history)))
        h = float(fleet_headroom_kw)
        if h <= 0:
            return 0.0
        return max(-h, min(h, self.script.get(tick.ts, 0.0)))


def synthetic_ticks(prices, loc="LZ_HOUSTON", t0=T0):
    """SYNTHETIC prices (hand cases only)."""
    return [PriceTick(t0 + k * FIVE, loc, float(p), None, None) for k, p in enumerate(prices)]


def fleet_of(specs, loc="LZ_HOUSTON"):
    """specs: (capacity_kwh, power_kw, soc, efficiency) per battery."""
    return Fleet.from_states(loc, [BatteryState(f"t{i}", c, p, s, e, True) for i, (c, p, s, e) in enumerate(specs)])


def run(ticks, fleet, strategy, *, start=0, lag=0, targets=None, trace=True):
    return replay_location(ticks, start, fleet, strategy, lag=lag, history_ticks=history_ticks(CFG), dt_h=DT,
                           soc_min=0.2, soc_max=1.0, targets=targets, trace=trace)


@lru_cache(maxsize=1)
def feed_2025() -> InMemoryPriceFeed:
    return snapshot_feed(years=(2025,), locations=("LZ_HOUSTON", "LZ_NORTH"))


def real_series(first_local_day: str, days: int, warmup_days: int = 7, locations=("LZ_HOUSTON", "LZ_NORTH")):
    """Real 5-min ticks: `days` local days from first_local_day, after `warmup_days` of warm-up."""
    from src.backtest.engine import local_midnight_utc
    from datetime import date

    start = local_midnight_utc(date.fromisoformat(first_local_day))
    return load_ticks(feed_2025(), locations, start, start + timedelta(days=days),
                      start - timedelta(days=warmup_days))


# --- hand-computed cases ---------------------------------------------------------------------


def test_one_battery_by_hand():
    # 10 kWh, 5 kW, eta 0.81 (sqrt 0.9), SoC 0.5; charge, hold, sell, sell at 20, 40, 100, 100 $/MWh.
    ticks = synthetic_ticks([20, 40, 100, 100])
    strategy = Scripted({ticks[0].ts: -5.0, ticks[2].ts: 5.0, ticks[3].ts: 5.0})
    r = run(ticks, fleet_of([(10.0, 5.0, 0.5, 0.81)]), strategy)
    kwh = 5.0 / 12  # 5 kW for 5 minutes
    assert r.delivered_kwh == pytest.approx([-kwh, 0.0, kwh, kwh], abs=1e-15)
    soc1 = 0.5 + kwh * 0.9 / 10  # charging stores 0.9 of the grid energy
    soc3 = soc1 - kwh / 0.9 / 10  # discharging drains grid energy / 0.9
    soc4 = soc3 - kwh / 0.9 / 10
    assert r.soc_trace[:, 0] == pytest.approx([soc1, soc1, soc3, soc4], abs=1e-15)
    assert r.revenue_usd[0] == pytest.approx(kwh * (-20 + 100 + 100) / 1000, abs=1e-15)  # $0.075
    assert r.revenue_usd[0] == pytest.approx(0.075, abs=1e-15)
    assert (r.sold_kwh[0], r.bought_kwh[0]) == (pytest.approx(2 * kwh), pytest.approx(kwh))
    ledger = BacktestLedger({"LZ_HOUSTON": 1})
    fill_ledger(ledger, [(ticks, r.delivered_kwh)])
    s = ledger.summary()
    assert s["total_usd"] == pytest.approx(0.075, abs=1e-15)
    assert (s["sold_kwh"], s["bought_kwh"]) == (pytest.approx(2 * kwh), pytest.approx(kwh))
    assert r.active_ticks == 3


def test_three_batteries_by_hand():
    # eta 0.81 (sqrt 0.9). b0 12 kWh/6 kW at 0.5; b1 12 kWh/3 kW at 0.5; b2 6 kWh/3 kW empty (at the 0.2 floor).
    ticks = synthetic_ticks([90, -10, 200])
    strategy = Scripted({ticks[0].ts: 6.0, ticks[1].ts: -12.0, ticks[2].ts: 12.0})
    r = run(ticks, fleet_of([(12.0, 6.0, 0.5, 0.81), (12.0, 3.0, 0.5, 0.81), (6.0, 3.0, 0.2, 0.81)]), strategy)
    # tick 0: sell 6 kW; headroom 6 + 3 + 0 = 9, so b0 gets 6*6/9 = 4 kW, b1 3*6/9 = 2 kW, b2 nothing.
    s0 = [0.5 - (4 / 12) / 0.9 / 12, 0.5 - (2 / 12) / 0.9 / 12, 0.2]
    # tick 1: buy 12 kW at -10 $/MWh (earns money); everyone has full charge headroom: -6, -3, -3 kW.
    s1 = [s0[0] + (6 / 12) * 0.9 / 12, s0[1] + (3 / 12) * 0.9 / 12, s0[2] + (3 / 12) * 0.9 / 6]
    # tick 2: sell 12 kW, but b2 can only give (0.2375 - 0.2) * 6 kWh * 0.9 per 5 min = 2.43 kW,
    # so the target is clipped to 6 + 3 + 2.43 = 11.43 kW and b2 lands exactly on the floor.
    b2_kw = (s1[2] - 0.2) * 6 * 0.9 * 12
    assert b2_kw == pytest.approx(2.43)
    s2 = [s1[0] - (6 / 12) / 0.9 / 12, s1[1] - (3 / 12) / 0.9 / 12, 0.2]
    assert r.feasible_kw == pytest.approx([6.0, -12.0, 6 + 3 + b2_kw], abs=1e-12)
    assert r.delivered_kwh == pytest.approx([6 / 12, -12 / 12, (9 + b2_kw) / 12], abs=1e-12)
    assert r.soc_trace == pytest.approx(np.array([s0, s1, s2]), abs=1e-12)
    assert r.soc_final[2] == 0.2  # the clamp puts it exactly on the floor, never below
    usd = (6 / 12) * 90 / 1000 + (-1.0) * -10 / 1000 + ((9 + b2_kw) / 12) * 200 / 1000
    assert usd == pytest.approx(0.2455)
    assert r.revenue_usd.sum() == pytest.approx(usd, abs=1e-12)
    assert r.revenue_usd[2] == pytest.approx(3 / 12 * 10 / 1000 + b2_kw / 12 * 200 / 1000, abs=1e-12)


def test_strategy_gets_the_seam_inputs_through_decision_tick():
    ticks = synthetic_ticks([10, 20, 30, 40, 50, 60])
    strategy = Scripted({})
    fleet = fleet_of([(10.0, 5.0, 0.5, 0.9), (20.0, 4.0, 0.25, 0.9)])
    run(ticks, fleet, strategy, start=4, lag=3)
    (tick4, soc4, h4, hist4), (tick5, _, _, hist5) = strategy.calls
    # the clock is the delivery tick's; the price is the one published `lag` ticks earlier
    assert (tick4.ts, tick4.price_usd_mwh, tick4.location) == (ticks[4].ts, 20.0, "LZ_HOUSTON")
    assert (tick5.ts, tick5.price_usd_mwh) == (ticks[5].ts, 30.0)
    assert hist4 == [10.0] and hist5 == [10.0, 20.0]  # strictly before the published tick
    assert h4 == 9.0  # symmetric H: sum of power_kw
    assert soc4 == pytest.approx((0.5 * 10 + 0.25 * 20) / 30)  # capacity-weighted


def test_nothing_published_yet_means_hold():
    ticks = synthetic_ticks([10, 20, 30])
    strategy = Scripted({t.ts: -5.0 for t in ticks})
    r = run(ticks, fleet_of([(10.0, 5.0, 0.5, 0.9)]), strategy, lag=2)
    assert len(strategy.calls) == 1  # only tick 2 has a published price (tick 0)
    assert list(r.target_kw) == [0.0, 0.0, -5.0]


def test_unavailable_batteries_get_nothing_and_do_not_count_in_h():
    ticks = synthetic_ticks([50, 50])
    states = [BatteryState("a", 10.0, 5.0, 0.5, 0.9, True), BatteryState("b", 10.0, 5.0, 0.5, 0.9, False)]
    strategy = Scripted({ticks[0].ts: 100.0})
    r = run(ticks, Fleet.from_states("LZ_HOUSTON", states), strategy)
    assert strategy.calls[0][2] == 5.0
    assert r.soc_trace[0, 1] == 0.5 and r.soc_trace[0, 0] < 0.5


# --- the fleet ------------------------------------------------------------------------------


def test_location_counts():
    assert location_counts(20000, {"A": 0.5, "B": 0.5}) == {"A": 10000, "B": 10000}
    assert location_counts(7, {"A": 0.5, "B": 0.5}) == {"A": 4, "B": 3}
    assert location_counts(10, {"A": 0.3, "B": 0.3, "C": 0.4}) == {"A": 3, "B": 3, "C": 4}
    assert sum(location_counts(12345, {"A": 0.37, "B": 0.63}).values()) == 12345


def test_build_fleets_is_the_seeded_fleet_in_contiguous_blocks():
    fleets = build_fleets(10, CFG.fleet.locations, CFG.battery, CFG.seed)
    states = generate_batteries(10, CFG.battery, CFG.seed)
    assert [f.size for f in fleets.values()] == [5, 5]
    ids = [b for f in fleets.values() for b in f.battery_ids]
    assert ids == [s.battery_id for s in states]
    caps = np.concatenate([f.capacity_kwh for f in fleets.values()])
    assert list(caps) == [s.capacity_kwh for s in states]
    assert np.all(np.concatenate([f.power_kw for f in fleets.values()]) <= caps)
    # the full fleet is the same battery population
    big = build_fleets(20000, CFG.fleet.locations, CFG.battery, CFG.seed)
    assert big["LZ_HOUSTON"].battery_ids[:5] == fleets["LZ_HOUSTON"].battery_ids
    assert big["LZ_NORTH"].battery_ids[0] == "b10000"


def test_year_bounds_are_operating_days():
    start, end = year_bounds(2025)
    assert start.isoformat() == "2025-01-01T06:00:00+00:00" and end.isoformat() == "2026-01-01T06:00:00+00:00"
    assert (end - start) / FIVE == 105120


# --- parity with the command-by-command path ---------------------------------------------------


def adapter_loop(ticks, start, states, strategy, lag):
    """The live path through the seams, one Command per battery per tick (like src/sim/skeleton.py)."""
    ids = [s.battery_id for s in states]
    adapter = InMemoryBatteryAdapter(states, tick_seconds=CFG.sim.tick_seconds)
    ledger = BacktestLedger({ticks[0].location: len(ids)})
    prices = [t.price_usd_mwh for t in ticks]
    window = history_ticks(CFG)
    delivered_out, soc_out = [], []
    for i in range(start, len(ticks)):
        before = [adapter.read(b) for b in ids]
        soc = np.array([s.soc for s in before])
        cap = np.array([s.capacity_kwh for s in before])
        power = np.array([s.power_kw for s in before])
        eff = np.array([s.efficiency for s in before])
        avail = np.array([s.available for s in before])
        seen = i - lag
        target = strategy.target_kw(decision_tick(ticks[i], ticks[seen]), fleet_soc(soc, cap, avail),
                                    fleet_headroom_kw(power, avail), prices[max(0, seen - window):seen]) \
            if seen >= 0 else 0.0
        discharge, charge = headroom_kw(soc, cap, power, eff, avail, DT, 0.2, 1.0)
        _, setpoints = allocate(target, discharge, charge)
        for b, sp in zip(ids, setpoints):
            adapter.apply(Command(make_command_id("parity", b, ticks[i].ts), b, float(sp), ticks[i].ts))
        after = np.array([adapter.read(b).soc for b in ids])
        kwh = float(grid_kwh_from_soc_change(soc, after, cap, eff).sum())
        ledger.record(ticks[i], kwh)
        delivered_out.append(kwh)
        soc_out.append(after)
    return np.array(delivered_out), np.array(soc_out), ledger.summary()


@pytest.mark.parametrize("name", ["bands", "peak"])
@pytest.mark.parametrize("lag", [0, 3])
def test_engine_is_bit_identical_to_the_command_loop(name, lag):
    series = real_series("2025-05-15", days=2, locations=("LZ_HOUSTON",))["LZ_HOUSTON"]
    states = generate_batteries(4, CFG.battery, CFG.seed)
    ref_kwh, ref_soc, ref_summary = adapter_loop(series.ticks, series.start, states,
                                                 make_strategy(CFG.strategy, 300, name), lag)
    fleet = Fleet.from_states("LZ_HOUSTON", states)
    for fast in (False, True):
        strategy = make_strategy(CFG.strategy, 300, name)
        targets = None
        if fast and uses_bands_fast_path(strategy):
            targets = bands_targets(series.prices, series.start, lag, fleet_headroom_kw(fleet.power_kw, fleet.available),
                                    bands_windows(series.prices, strategy, history_ticks(CFG)))
        r = run(series.ticks, fleet, strategy, start=series.start, lag=lag, targets=targets)
        assert np.array_equal(r.delivered_kwh, ref_kwh)
        assert np.array_equal(r.soc_trace, ref_soc)
        ledger = BacktestLedger({"LZ_HOUSTON": 4})
        fill_ledger(ledger, [(series.replayed, r.delivered_kwh)])
        assert ledger.summary() == ref_summary
    assert r.active_ticks > 20  # the window really trades


# --- equivalence with the M0 skeleton ----------------------------------------------------------


@pytest.mark.parametrize("name,lag,usd", [("bands", 3, 0.5097), ("bands", 0, 0.6255), ("peak", 3, 4.4470),
                                          ("peak", 0, 4.4470)])
@pytest.mark.parametrize("fast", [False, True])
def test_identical_to_the_m0_skeleton(name, lag, usd, fast):
    sk = skeleton.run(CFG, strategy_name=name, lag=lag)
    feed = InMemoryPriceFeed.from_intervals(m0_prices.interval_rows())
    series = load_ticks(feed, ["LZ_HOUSTON"], m0_prices.DAY_START_UTC, m0_prices.DAY_END_UTC,
                        m0_prices.WARMUP_START_UTC)
    fleets = build_fleets(1, {"LZ_HOUSTON": 1.0}, CFG.battery, CFG.seed)
    rep = replay(name, CFG, series, fleets, lag=lag, fast=fast, trace=True)
    assert rep.fast_path == (fast and name == "bands")
    assert rep.summary == sk.summary  # every float identical, not approximately
    assert round(rep.summary["total_usd"], 4) == usd
    run_ = rep.runs["LZ_HOUSTON"]
    assert list(run_.delivered_kwh) == [row.delivered_kwh for row in sk.rows]
    assert list(run_.soc_trace[:, 0]) == [row.soc for row in sk.rows]
    assert list(run_.target_kw) == [row.target_kw for row in sk.rows]
    assert run_.soc_final[0] == sk.final.soc


# --- invariants on real prices --------------------------------------------------------------


@pytest.fixture(scope="module")
def two_weeks():
    return real_series("2025-07-07", days=14)


@pytest.mark.parametrize("name", ["bands", "peak"])
def test_invariants_on_real_prices(two_weeks, name):
    fleets = build_fleets(60, CFG.fleet.locations, CFG.battery, CFG.seed)
    rep = replay(name, CFG, two_weeks, fleets, lag=3, trace=True)
    for loc, r in rep.runs.items():
        f = fleets[loc]
        # SoC stays in bounds after every tick
        assert r.soc_trace.min() >= 0.2 and r.soc_trace.max() <= 1.0
        # per-battery grid energy per tick within power_kw * dt, and same sign as the fleet target
        before = np.vstack([f.soc_init, r.soc_trace[:-1]])
        kwh = grid_kwh_from_soc_change(before, r.soc_trace, f.capacity_kwh, f.efficiency)
        assert np.all(np.abs(kwh) <= f.power_kw * DT + 1e-12)
        assert np.all(kwh * np.sign(r.target_kw)[:, None] >= -1e-15)
        assert np.allclose(kwh.sum(axis=1), r.delivered_kwh, rtol=0, atol=1e-12)
        # storage balance with eta: change in stored kWh = bought * sqrt(eta) - sold / sqrt(eta)
        root = np.sqrt(f.efficiency)
        stored = (r.soc_final - f.soc_init) * f.capacity_kwh
        assert np.allclose(stored, r.bought_kwh * root - r.sold_kwh / root, rtol=0, atol=1e-9)
        assert np.allclose(r.sold_kwh - r.bought_kwh, kwh.sum(axis=0), rtol=0, atol=1e-9)
        # the ledger values each tick's energy at that tick's own RT price
        prices = np.array([t.price_usd_mwh for t in two_weeks[loc].replayed])
        assert rep.summary["per_location_usd"][loc] == pytest.approx(float((r.delivered_kwh * prices).sum()) / 1000,
                                                                     rel=1e-12)
        assert r.revenue_usd.sum() == pytest.approx(rep.summary["per_location_usd"][loc], rel=1e-9)
        assert r.active_ticks > 0
    m = rep.metrics
    assert m["checks"]["storage_balance_max_abs_kwh"] < 1e-9
    assert m["per_battery_usd_year"] == pytest.approx(rep.summary["total_usd"] / 60)
    assert m["sold_kwh"] == pytest.approx(sum(r.sold_kwh.sum() for r in rep.runs.values()))


# --- no look-ahead -----------------------------------------------------------------------------


@pytest.mark.parametrize("name,fast", [("bands", False), ("bands", True), ("peak", False)])
@pytest.mark.parametrize("lag", [0, 3])
def test_shuffling_future_prices_changes_no_earlier_decision(two_weeks, name, fast, lag):
    loc = "LZ_NORTH"
    series = two_weeks[loc]
    fleets = {loc: build_fleets(40, CFG.fleet.locations, CFG.battery, CFG.seed)[loc]}
    cut = series.start + 7 * 288 + 100  # prices after this index get shuffled
    rng = np.random.default_rng(7)
    tail = series.ticks[cut + 1:]
    shuffled_prices = rng.permutation([t.price_usd_mwh for t in tail])
    shuffled = series.ticks[:cut + 1] + [PriceTick(t.ts, t.location, float(p), None, None)
                                         for t, p in zip(tail, shuffled_prices)]
    from src.backtest.engine import TickSeries

    a = replay(name, CFG, {loc: series}, fleets, lag=lag, fast=fast).runs[loc]
    b = replay(name, CFG, {loc: TickSeries(loc, shuffled, series.start)}, fleets, lag=lag, fast=fast).runs[loc]
    same = cut + lag - series.start + 1  # decisions up to tick cut + lag only saw prices up to `cut`
    for field in ("target_kw", "feasible_kw", "delivered_kwh", "fleet_soc"):
        assert np.array_equal(getattr(a, field)[:same], getattr(b, field)[:same]), field
    if name == "bands":
        assert not np.array_equal(a.target_kw, b.target_kw)  # the shuffle did change later decisions


# --- the bands fast path ---------------------------------------------------------------------


@pytest.mark.parametrize("lag", [0, 3])
def test_bands_fast_path_equals_the_seam_every_tick(two_weeks, lag):
    strategy = BandsStrategy()
    for loc, series in two_weeks.items():
        prices = series.prices
        h = 12345.6
        fast = bands_targets(prices, series.start, lag, h, bands_windows(prices, strategy, history_ticks(CFG)))
        p = list(prices)
        seam = [strategy.target_kw(decision_tick(series.ticks[i], series.ticks[i - lag]), 0.5, h,
                                   p[max(0, i - lag - history_ticks(CFG)):i - lag])
                for i in range(series.start, len(p))]
        assert np.array_equal(fast, np.array(seam)), loc
        assert (fast < 0).any() and (fast > 0).any()


def test_bands_fast_path_while_the_window_fills_and_with_no_headroom():
    series = real_series("2025-02-01", days=3, warmup_days=1, locations=("LZ_HOUSTON",))["LZ_HOUSTON"]
    strategy = BandsStrategy(window_ticks=2016, min_samples=288)
    prices, hist = series.prices, history_ticks(CFG)
    windows = bands_windows(prices, strategy, hist)
    for lag in (0, 3):
        fast = bands_targets(prices, 0, lag, 50.0, windows)  # replay from the very first tick
        p = list(prices)
        seam = [strategy.target_kw(decision_tick(series.ticks[i], series.ticks[i - lag]), 0.5, 50.0,
                                   p[max(0, i - lag - hist):i - lag]) if i - lag >= 0 else 0.0
                for i in range(len(p))]
        assert np.array_equal(fast, np.array(seam))
        assert not fast[:288 + lag].any()  # holds until one day of history exists
        assert fast[288 + lag:].any()
    assert not bands_targets(prices, 0, 3, 0.0, windows).any()


@pytest.mark.parametrize("seed", range(4))
def test_bands_fast_path_equals_the_seam_on_awkward_prices(seed):
    """SYNTHETIC prices: many exact zeros, negatives, repeats and flat stretches, so p90 <= 0,
    p25 == p90 ties and price == percentile cases all occur."""
    rng = np.random.default_rng(seed)
    prices = rng.choice([-5.0, 0.0, 0.0, 0.0, 10.0, 25.0, 25.0, 300.0], size=1500)
    prices[rng.random(1500) < 0.35] = 0.0
    prices[400:520] = 0.0  # a flat stretch: p25 == p90 == 0, so charge wins the tie
    prices[700:1100] = rng.choice([-5.0, -5.0, -5.0, 0.0, 0.0], size=400)  # p25 < 0 == p90: a $0 price must hold
    ticks = synthetic_ticks(prices)
    strategy = BandsStrategy(window_ticks=300, min_samples=50)
    for hist in (250, 400):  # history shorter and longer than the strategy's window
        windows = bands_windows(prices, strategy, hist)
        for lag in (0, 3):
            fast = bands_targets(prices, 0, lag, 7.0, windows)
            seam = [strategy.target_kw(decision_tick(ticks[i], ticks[i - lag]), 0.5, 7.0,
                                       list(prices[max(0, i - lag - hist):i - lag])) if i - lag >= 0 else 0.0
                    for i in range(len(prices))]
            assert np.array_equal(fast, np.array(seam)), (hist, lag)
    decided_by_price_above_zero = [
        i for i in range(300, len(prices))
        if prices[i] == 0.0 and prices[i] > np.percentile(prices[i - 300:i], 25)
        and prices[i] >= np.percentile(prices[i - 300:i], 90)]
    assert decided_by_price_above_zero  # the "and price > 0" part of the rule is really exercised


def test_metrics_definitions_by_hand():
    from src.backtest.engine import TickSeries

    ticks = synthetic_ticks([20, 40, 100, 100])  # the one-battery hand case above
    strategy = Scripted({ticks[0].ts: -5.0, ticks[2].ts: 5.0, ticks[3].ts: 5.0})
    fleet = fleet_of([(10.0, 5.0, 0.5, 0.81)])
    rep = replay("scripted", CFG, {"LZ_HOUSTON": TickSeries("LZ_HOUSTON", ticks, 0)}, {"LZ_HOUSTON": fleet},
                 lag=0, strategies={"LZ_HOUSTON": strategy}, expected_ticks=4)
    m, kwh = rep.metrics, 5.0 / 12
    assert m["fleet_usd_year"] == pytest.approx(0.075) and m["per_battery_usd_year"] == pytest.approx(0.075)
    assert m["usd_per_kw_year"] == pytest.approx(0.075 / 5.0)  # per kW of power_kw
    assert m["usd_per_kwh_year"] == pytest.approx(0.075 / 10.0)  # per nameplate kWh
    out = 2 * kwh / 0.9  # kWh that left storage for 2 x 5/12 kWh delivered
    assert m["cycles_per_year"] == pytest.approx(out / (10.0 * 0.8))  # usable = 100% down to the 20% reserve
    assert m["cycles_per_year_nameplate"] == pytest.approx(out / 10.0)
    assert (m["sold_kwh"], m["bought_kwh"]) == (pytest.approx(2 * kwh), pytest.approx(kwh))
    assert (m["avg_sell_usd_mwh"], m["avg_buy_usd_mwh"]) == (pytest.approx(100.0), pytest.approx(20.0))
    assert m["top_days"] == [{"date_local": "2025-03-03", "usd": pytest.approx(0.075)}]
    assert m["coverage"]["fraction"] == 1.0 and m["monthly_usd"] == {"2025-03": pytest.approx(0.075)}


def test_fast_path_only_for_the_exact_v1_class():
    class Tweaked(BandsStrategy):
        pass

    assert uses_bands_fast_path(BandsStrategy())
    assert not uses_bands_fast_path(Tweaked())
    assert not uses_bands_fast_path(PeakStrategy())


# --- the ledger's record order ----------------------------------------------------------------


class RecordingLedger(BacktestLedger):
    def __init__(self, fleet):
        super().__init__(fleet)
        self.order = []

    def record(self, tick, delivered_kwh):
        self.order.append((tick.ts, tick.location))
        super().record(tick, delivered_kwh)


def test_fill_ledger_records_ticks_in_order_and_locations_in_config_order():
    a = synthetic_ticks([10, 20, 30], loc="LZ_NORTH")
    b = synthetic_ticks([40, 50, 60], loc="LZ_HOUSTON")
    ledger = RecordingLedger({"LZ_NORTH": 1, "LZ_HOUSTON": 1})
    fill_ledger(ledger, [(a, np.ones(3)), (b, np.ones(3))])
    assert ledger.order == [(t.ts, loc) for t in a for loc in ("LZ_NORTH", "LZ_HOUSTON")]
    # a gap at one location: still in time order
    ledger = RecordingLedger({"LZ_NORTH": 1, "LZ_HOUSTON": 1})
    fill_ledger(ledger, [(a[:1] + a[2:], np.ones(2)), (b, np.ones(3))])
    assert [ts for ts, _ in ledger.order] == sorted(ts for ts, _ in ledger.order)
    assert ledger.summary()["total_usd"] == pytest.approx((10 + 30 + 40 + 50 + 60) / 1000)


def test_replay_rejects_bad_inputs():
    ticks = synthetic_ticks([10, 20])
    fleet = fleet_of([(10.0, 5.0, 0.5, 0.9)])
    with pytest.raises(ValueError):
        run(ticks, fleet, Scripted({}), lag=-1)
    with pytest.raises(ValueError):
        run(ticks, fleet, None)
    with pytest.raises(ValueError):
        run(ticks, fleet, None, targets=np.zeros(3))

    class NotANumber:  # SYNTHETIC broken strategy: allocate() must refuse a non-finite target
        def target_kw(self, tick, fleet_soc, fleet_headroom_kw, price_history):
            return math.nan

    with pytest.raises(ValueError):
        run(ticks, fleet, NotANumber())
