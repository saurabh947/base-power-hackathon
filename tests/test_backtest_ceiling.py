"""The perfect-hindsight ceiling (src/backtest/ceiling.py).

Hand-computed LPs use SYNTHETIC two-interval price pairs. The scaling, concavity and bracket
checks use real ERCOT days from the committed snapshot.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pytest
from scipy.optimize import linprog

from src.backtest import ceiling
from src.backtest.ceiling import days_from_ticks, interpolate_bounds, ratio_grid, solve_day
from src.backtest.engine import build_fleets, load_ticks, local_midnight_utc
from src.backtest.runner import Context, backtest_settings
from src.config import load_config
from src.feeds.parquet import snapshot_feed

CFG = load_config()
PHYS = {"efficiency": 0.81, "soc_min": 0.2, "soc_max": 1.0}  # sqrt(eta) = 0.9 keeps the hand math short


@pytest.fixture(scope="module")
def year_days():
    feed = snapshot_feed(years=(2025,), locations=("LZ_HOUSTON",))
    start, end = local_midnight_utc(date(2025, 1, 1)), local_midnight_utc(date(2026, 1, 1))
    ticks = list(feed.ticks(start, end, "LZ_HOUSTON"))
    return days_from_ticks(ticks, start, end)


# --- hand-computed LPs (SYNTHETIC prices) ---------------------------------------------------


def test_buy_low_sell_high_by_hand():
    # Per kWh of capacity: fill 0.2 -> 1.0 in the $10 interval, empty it in the $100 one.
    day = solve_day([10.0, 100.0], 4.0, schedule=True, **PHYS)
    c0 = 0.8 / (0.25 * 0.9)  # grid kW that stores 0.8 kWh in 15 minutes
    d1 = 0.8 * 0.9 / 0.25  # grid kW that drains 0.8 kWh in 15 minutes
    assert day.charge == pytest.approx([c0, 0.0], abs=1e-7) and day.discharge == pytest.approx([0.0, d1], abs=1e-7)
    assert day.revenue_per_kwh == pytest.approx(0.25 * (100 * d1 - 10 * c0) / 1000, abs=1e-9)  # $0.0631
    assert day.discharged_per_kwh == pytest.approx(0.8, abs=1e-9)
    # power-limited: ratio 1 moves only 0.225 kWh per kWh of capacity
    assert solve_day([10.0, 100.0], 1.0, **PHYS).revenue_per_kwh == pytest.approx(0.25 * (100 * 0.81 - 10) / 1000)
    # one tenth of a cycle a day
    capped = solve_day([10.0, 100.0], 4.0, cycles_cap=0.1, **PHYS)
    assert capped.discharged_per_kwh == pytest.approx(0.08, abs=1e-9)
    assert capped.revenue_per_kwh == pytest.approx(day.revenue_per_kwh / 10, abs=1e-9)


def test_start_level_is_chosen_and_the_day_is_cyclic():
    # sell first, buy back later: only possible because the day may start full (and must end full)
    day = solve_day([100.0, 10.0], 4.0, schedule=True, **PHYS)
    assert day.revenue_per_kwh == pytest.approx(solve_day([10.0, 100.0], 4.0, **PHYS).revenue_per_kwh, abs=1e-9)
    assert day.level[0] == pytest.approx(1.0) and day.level[-1] == pytest.approx(day.level[0], abs=1e-9)


def test_no_trade_when_the_spread_does_not_beat_the_losses():
    assert solve_day([95.0, 100.0], 4.0, **PHYS).revenue_per_kwh == pytest.approx(0.0, abs=1e-12)  # 100*0.81 < 95


def test_intervals_without_a_price_stay_idle():
    day = solve_day([10.0, 100.0, 5.0, 500.0], 4.0, tradable=[True, False, True, True], schedule=True, **PHYS)
    assert day.charge[1] == 0 and day.discharge[1] == 0
    assert day.revenue_per_kwh == pytest.approx(solve_day([5.0, 500.0], 4.0, **PHYS).revenue_per_kwh, abs=1e-9)


# --- real days --------------------------------------------------------------------------------


def test_days_are_operating_days_with_dst(year_days):
    assert len(year_days) == 365
    lengths = {d.date_local: len(d.prices) for d in year_days}
    assert lengths["2025-03-09"] == 92 and lengths["2025-11-02"] == 100 and lengths["2025-05-16"] == 96
    assert all(d.tradable.all() for d in year_days)  # the snapshot has no gaps
    may16 = next(d for d in year_days if d.date_local == "2025-05-16")
    assert may16.prices.max() == 2250.15  # the M0 day's spike (src/sim/m0_prices.py)


def test_no_simultaneous_charge_and_discharge_at_positive_prices(year_days):
    for d in year_days[100:105]:
        s = solve_day(d.prices, 0.5, schedule=True, **PHYS)
        positive = d.prices > 0
        assert np.all(np.minimum(s.charge, s.discharge)[positive] < 1e-7)
        assert np.all(s.charge + s.discharge <= 0.5 + 1e-7)
        assert s.level.min() >= 0.2 - 1e-9 and s.level.max() <= 1.0 + 1e-9


def absolute_lp(prices, capacity, power, eta=0.81, lo=0.2, hi=1.0, dt=0.25):
    """An independent dense LP in absolute kW/kWh (no scaling) for one battery and one day."""
    n = len(prices)
    r = np.sqrt(eta)
    lower = np.tril(np.ones((n, n)))
    # x = [c(n), d(n), e0]; level after k intervals = e0 + dt * lower @ (r c - d / r)
    a_level = np.hstack([dt * r * lower, -dt / r * lower, np.ones((n, 1))])
    a_ub = np.vstack([a_level, -a_level, np.hstack([np.eye(n), np.eye(n), np.zeros((n, 1))])])
    b_ub = np.concatenate([np.full(n, hi * capacity), np.full(n, -lo * capacity), np.full(n, power)])
    a_eq = np.hstack([np.full((1, n), dt * r), np.full((1, n), -dt / r), np.zeros((1, 1))])
    cost = np.concatenate([np.asarray(prices) / 1000 * dt, -np.asarray(prices) / 1000 * dt, [0.0]])
    res = linprog(cost, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=[0.0],
                  bounds=[(0, power)] * (2 * n) + [(lo * capacity, hi * capacity)], method="highs")
    assert res.status == 0
    return -res.fun


def test_revenue_scales_with_capacity_at_a_fixed_power_ratio(year_days):
    rng = np.random.default_rng(3)
    for d in [year_days[i] for i in (10, 135, 200)]:
        for capacity, power in [(10.0, 5.0), (20.0, 5.0), (14.39, 10.03)] + [tuple(rng.uniform((10, 5), (20, 11.5)))
                                                                             for _ in range(2)]:
            power = min(power, capacity)
            direct = absolute_lp(d.prices, capacity, power)
            scaled = capacity * solve_day(d.prices, power / capacity, **PHYS).revenue_per_kwh
            assert scaled == pytest.approx(direct, rel=1e-6, abs=1e-9)


def test_curve_is_nondecreasing_and_concave_so_the_bracket_holds(year_days):
    days = year_days[120:130]
    grid = np.round(np.arange(0.25, 1.0001, 0.125), 6)

    def f(r):
        return sum(solve_day(d.prices, float(r), **PHYS).revenue_per_kwh for d in days)

    values = np.array([f(r) for r in grid])
    assert np.all(np.diff(values) >= -1e-9)
    assert np.all(np.diff(values, 2) <= 1e-9)
    xs = np.array([0.3, 0.41, 0.55, 0.8, 0.97])
    mid, low, high = interpolate_bounds(grid, values, xs)
    exact = np.array([f(x) for x in xs])
    assert np.all(low <= exact + 1e-9) and np.all(exact <= high + 1e-9)
    assert np.all(high - low < 0.05 * exact)


def test_interpolate_bounds_on_a_known_concave_curve():
    grid = np.linspace(0.25, 1.0, 7)
    xs = np.linspace(0.25, 1.0, 101)
    mid, low, high = interpolate_bounds(grid, np.sqrt(grid), xs)
    assert np.all(low <= np.sqrt(xs) + 1e-15) and np.all(np.sqrt(xs) <= high + 1e-15)
    assert mid[0] == pytest.approx(0.5) and high[-1] == pytest.approx(1.0)


def test_ratio_grid_covers_the_fleet():
    fleets = build_fleets(20000, CFG.fleet.locations, CFG.battery, CFG.seed)
    ratios = np.concatenate([f.power_kw / f.capacity_kwh for f in fleets.values()])
    grid = ratio_grid(ratios, 0.05)
    assert grid[0] <= ratios.min() and grid[-1] >= ratios.max()
    assert grid[0] == pytest.approx(0.25) and grid[-1] == pytest.approx(1.0) and len(grid) == 16


# --- the record -----------------------------------------------------------------------------


def small_context(tmp_settings=None, days=14, size=30):
    feed = snapshot_feed(years=(2025,), locations=tuple(CFG.fleet.locations))
    start = local_midnight_utc(date(2025, 6, 1))
    end = start + timedelta(days=days)
    series = load_ticks(feed, list(CFG.fleet.locations), start, end, start - timedelta(days=7))
    settings = backtest_settings()
    settings["ceiling"].update(ratio_step=0.25, workers=1)
    settings.update(tmp_settings or {})
    fleets = build_fleets(size, CFG.fleet.locations, CFG.battery, CFG.seed)
    return Context(CFG, settings, 2025, series, fleets, "snapshot (test)", start, end, days * 288)


def test_ceiling_record_end_to_end_and_cached(tmp_path):
    ctx = small_context()
    rec = ceiling.ceiling_record(ctx, out_dir=tmp_path, log=lambda s: None)
    m = rec["metrics"]
    assert rec["strategy"] == "hindsight" and rec["label"] == "Best possible (perfect hindsight)"
    assert m["headline"] == "uncapped" and rec["per_battery_avg_usd"] == m["uncapped"]["per_battery_usd_year"]
    assert 0 < m["capped"]["fleet_usd_year"] <= m["uncapped"]["fleet_usd_year"]
    assert m["capped"]["cycles_per_year"] <= 14 * 1.0 + 1e-6  # at most one cycle on each of the 14 days
    b = m["uncapped"]["bracket_fleet_usd"]
    assert b["low"] <= m["uncapped"]["fleet_usd_year"] + 1e-9 <= b["high"] + 2e-9
    assert sum(rec["per_location"].values()) == pytest.approx(rec["fleet_total_usd"])
    assert m["days"] == {loc: 14 for loc in CFG.fleet.locations} and m["scope"].startswith("all batteries")
    cached = list((tmp_path / "cache").glob("ceiling_curves_*.json"))
    assert len(cached) == 1
    again = ceiling.ceiling_record(ctx, out_dir=tmp_path, log=lambda s: None)
    assert again["metrics"]["uncapped"] == m["uncapped"] and again["metrics"]["cache_key"] == m["cache_key"]
    json.dumps(rec, allow_nan=False)  # JSON-ready for backtest_runs
    # exact for sample batteries: C * F(P/C) with F solved at the battery's own ratio
    f = ctx.fleets["LZ_NORTH"]
    days = days_from_ticks(ctx.series["LZ_NORTH"].replayed, ctx.start, ctx.end)
    for i in (0, 7):
        exact = f.capacity_kwh[i] * sum(solve_day(d.prices, f.power_kw[i] / f.capacity_kwh[i],
                                                  efficiency=0.9, soc_min=0.2, soc_max=1.0).revenue_per_kwh
                                        for d in days)
        grid = np.array(m["curves"]["LZ_NORTH"]["ratio"])
        _, lo, hi = interpolate_bounds(grid, np.array(m["curves"]["LZ_NORTH"]["uncapped_usd"]),
                                       np.array([f.power_kw[i] / f.capacity_kwh[i]]))
        assert f.capacity_kwh[i] * lo[0] - 1e-9 <= exact <= f.capacity_kwh[i] * hi[0] + 1e-9


def test_parallel_curves_equal_serial():
    ctx = small_context(days=3)
    days = {loc: days_from_ticks(ctx.series[loc].replayed, ctx.start, ctx.end) for loc in ctx.fleets}
    grid = np.array([0.25, 0.5, 1.0])
    serial = ceiling.curves(days, grid, efficiency=0.9, soc_min=0.2, soc_max=1.0, cycles_cap=1.0, workers=1)
    parallel = ceiling.curves(days, grid, efficiency=0.9, soc_min=0.2, soc_max=1.0, cycles_cap=1.0, workers=2)
    assert serial == parallel
