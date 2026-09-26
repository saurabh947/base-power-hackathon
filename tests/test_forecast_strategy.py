"""ForecastStrategy rules and the Strategy seam contract, on SYNTHETIC forecast books.

The books below are SYNTHETIC (labelled): hand-made p50/p90 curves, so each
rule can be checked by hand. The contract suite (tests/test_contracts_strategy.py)
also runs the strategy on the real cached forecasts when they are present.
"""
import copy
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.config import load_config
from src.core.models import PriceTick
from src.forecast import store
from src.forecast.provider import ForecastBook
from src.strategy import make_strategy
from src.strategy.forecast import ForecastStrategy, fleet_duration_h

UTC = timezone.utc
T = pd.Timestamp("2025-05-16T12:00Z")
H = 1000.0
DURATION = 1.5  # SYNTHETIC fleet: 1.5 kWh per kW


def book_from(p50, issued=T, p90=None, location="LZ_HOUSTON", step=15):
    """SYNTHETIC book with one forecast issued at `issued`, points from `issued`."""
    p50 = np.asarray(p50, dtype=np.float32)
    p90 = p50 + 20 if p90 is None else np.asarray(p90, dtype=np.float32)
    row = store.ForecastRow(pd.Timestamp(issued), location, "rt_price", "synthetic", pd.Timestamp(issued), step,
                            p50 - 20, p50, p90)
    return ForecastBook.from_frame(store._frame([row]), location, "rt_price", "synthetic")


def strategy(p50, **kw):
    return ForecastStrategy({"LZ_HOUSTON": book_from(p50, **{k: kw.pop(k) for k in ("issued", "p90") if k in kw})},
                            duration_h=DURATION, **kw)


def tick(price, ts=T + pd.Timedelta(minutes=5), location="LZ_HOUSTON"):
    return PriceTick(pd.Timestamp(ts).to_pydatetime(), location, float(price), None, None)


# SYNTHETIC day: 48 quarter-hours (12 h) climbing from 20 to 67 $/MWh
RAMP = 20.0 + np.arange(48)


def test_k_is_the_slots_to_move_a_full_battery():
    s = strategy(RAMP)
    # usable 0.8 * 1.5 h = 1.2 h; discharge delivers * sqrt(0.9); charge needs / sqrt(0.9); 12 slots per hour
    assert s.k_sell == math.ceil(0.8 * 1.5 * math.sqrt(0.9) * 12)  # 14
    assert s.k_buy == math.ceil(0.8 * 1.5 / math.sqrt(0.9) * 12)  # 16


def test_sells_only_among_the_k_best_slots_ahead():
    s = strategy(RAMP)  # 48 points ahead = 144 slots; k_sell = 14 slots = 4.67 points
    top = np.sort(RAMP)[::-1]
    # 4 points (12 slots) above: 12 < 14 -> sell; 5 points (15 slots) above -> hold
    assert s.target_kw(tick(top[4] + 0.5), 0.6, H, []) == H
    assert s.target_kw(tick(top[5] + 0.5), 0.6, H, []) == 0.0


def test_buys_only_among_the_k_cheapest_slots_ahead():
    s = strategy(RAMP)  # k_buy = 16 slots = 5.33 points
    assert s.target_kw(tick(RAMP[5] - 0.5), 0.6, H, []) == -H  # 5 points (15 slots) below
    assert s.target_kw(tick(RAMP[6] - 0.5), 0.6, H, []) == 0.0  # 6 points (18 slots) below


def test_never_sells_at_or_below_zero_and_charges_on_negative_prices():
    s = strategy(np.full(48, -30.0))
    assert s.target_kw(tick(0.0), 0.6, H, []) == 0.0
    assert s.target_kw(tick(-5.0), 0.6, H, []) == 0.0  # above every forecast, but not > 0
    assert strategy(RAMP).target_kw(tick(-40.0), 0.6, H, []) == -H


def test_charge_needs_a_sale_that_covers_the_losses():
    flat = np.full(48, 30.0)
    s = strategy(flat)
    # below every forecast, but 29 > 0.9 * 30: a round trip would lose money
    assert s.target_kw(tick(29.0), 0.6, H, []) == 0.0
    assert s.target_kw(tick(26.9), 0.6, H, []) == -H
    assert strategy(flat, charge_margin=1e6).target_kw(tick(29.0), 0.6, H, []) == -H


def test_empty_fleet_does_not_sell_and_full_fleet_does_not_buy():
    s = strategy(RAMP)
    assert s.target_kw(tick(500.0), 0.2, H, []) == 0.0
    assert s.target_kw(tick(500.0), 0.2000001, H, []) == 0.0
    assert s.target_kw(tick(500.0), 0.21, H, []) == H
    assert s.target_kw(tick(-50.0), 1.0, H, []) == 0.0
    assert s.target_kw(tick(-50.0), 0.99, H, []) == -H


def test_window_is_the_horizon_ahead_only():
    # SYNTHETIC: a 24-h forecast whose second 12 h hold a 500 $/MWh plateau
    p50 = np.concatenate([np.full(48, 40.0), np.full(48, 500.0)])
    assert strategy(p50, horizon_hours=12).target_kw(tick(45.0), 0.6, H, []) == H  # plateau is out of view
    assert strategy(p50, horizon_hours=24).target_kw(tick(45.0), 0.6, H, []) == 0.0
    # points already over are ignored: 11 h in, the plateau fills the 12 h window, so 45 is a buy
    late = T + pd.Timedelta(hours=11, minutes=5)
    assert strategy(p50, horizon_hours=12, max_age_hours=12).target_kw(tick(45.0, late), 0.6, H, []) == -H


def test_holds_without_a_usable_forecast():
    s = strategy(RAMP)
    assert s.target_kw(tick(500.0, T - pd.Timedelta(minutes=5)), 0.6, H, []) == 0.0  # before the first issue
    assert s.target_kw(tick(500.0, T + pd.Timedelta(hours=6, minutes=5)), 0.6, H, []) == 0.0  # stale (> 6 h)
    assert s.target_kw(tick(500.0, location="LZ_NORTH"), 0.6, H, []) == 0.0  # no book for this location
    few = strategy(RAMP[:6])  # fewer than min_points ahead
    assert few.target_kw(tick(500.0), 0.6, H, []) == 0.0


def test_uses_only_forecasts_issued_by_the_decision_time():
    early = book_from(np.full(96, 40.0), issued=T)
    late_row = store.ForecastRow(T + pd.Timedelta(hours=1), "LZ_HOUSTON", "rt_price", "synthetic",
                                 T + pd.Timedelta(hours=1), 15, *(np.full(96, 900.0, np.float32),) * 3)
    both = ForecastBook.from_frame(store._frame([
        store.ForecastRow(T, "LZ_HOUSTON", "rt_price", "synthetic", T, 15, early.p10[0], early.p50[0], early.p90[0]),
        late_row]), "LZ_HOUSTON", "rt_price", "synthetic")
    s = ForecastStrategy({"LZ_HOUSTON": both}, duration_h=DURATION)
    just_before = T + pd.Timedelta(hours=1) - pd.Timedelta(microseconds=1)
    assert s.target_kw(tick(100.0, just_before), 0.6, H, []) == H  # 40 $/MWh ahead: sell at 100
    assert s.target_kw(tick(100.0, T + pd.Timedelta(hours=1)), 0.6, H, []) == -H  # 900 ahead: buy at 100


def test_optional_reserve_holds_back_for_a_p90_spike():
    p90 = RAMP + 20
    p90[30] = 800.0  # SYNTHETIC spike warning
    kw = {"reserve_spike_usd": 300.0, "reserve_soc": 0.5}
    assert strategy(RAMP, p90=p90).target_kw(tick(200.0), 0.4, H, []) == H  # reserve off by default
    assert strategy(RAMP, p90=p90, **kw).target_kw(tick(200.0), 0.4, H, []) == 0.0
    assert strategy(RAMP, p90=p90, **kw).target_kw(tick(200.0), 0.6, H, []) == H  # above the reserve
    assert strategy(RAMP, p90=p90, **kw).target_kw(tick(900.0), 0.4, H, []) == H  # the spike is here


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_holds_on_non_finite_inputs(bad):
    s = strategy(RAMP)
    assert s.target_kw(tick(bad), 0.6, H, []) == 0.0
    assert s.target_kw(tick(500.0), bad, H, []) == 0.0
    assert s.target_kw(tick(500.0), 0.6, bad, []) == 0.0


def test_naive_tick_raises():
    with pytest.raises(ValueError):
        strategy(RAMP).target_kw(PriceTick(datetime(2025, 5, 16, 12, 5), "LZ_HOUSTON", 50.0, None, None), 0.6, H, [])


# --- the generic Strategy contract on a SYNTHETIC full-year book --------------------------


@pytest.fixture(scope="module")
def year_strategy():
    """SYNTHETIC: hourly forecasts across 2025 with a daily sine shape (not real prices)."""
    issued = pd.date_range("2025-01-01T06:00Z", "2026-01-01T06:00Z", freq="h", inclusive="left")
    rows = []
    for t in issued:
        hours = t.hour + np.arange(96) / 4
        p50 = (40 + 30 * np.sin(2 * np.pi * (hours - 14) / 24)).astype(np.float32)
        rows.append(store.ForecastRow(t, "LZ_HOUSTON", "rt_price", "synthetic", t, 15, p50 - 15, p50, p50 + 40))
    book = ForecastBook.from_frame(store._frame(rows), "LZ_HOUSTON", "rt_price", "synthetic")
    return lambda: ForecastStrategy({"LZ_HOUSTON": book}, duration_h=DURATION)


def contract_cases():
    from tests.test_contracts_strategy import random_cases

    return list(random_cases())


def test_contract_bounded_finite_float(year_strategy):
    s = year_strategy()
    actions = set()
    for t, soc, headroom, history in contract_cases():
        out = s.target_kw(t, soc, headroom, history)
        assert isinstance(out, float) and math.isfinite(out) and -headroom <= out <= headroom
        actions.add(np.sign(out) if headroom else 0)
        assert s.target_kw(t, soc, 0.0, history) == 0.0
    assert actions == {-1.0, 0, 1.0}  # every branch was exercised


def test_contract_pure_and_order_independent(year_strategy):
    s, fresh = year_strategy(), year_strategy()
    cases = contract_cases()[:60]
    first = []
    for t, soc, headroom, history in cases:
        before = copy.deepcopy(history)
        first.append(s.target_kw(t, soc, headroom, history))
        assert history == before
    for (t, soc, headroom, history), expected in zip(reversed(cases), reversed(first)):
        assert s.target_kw(t, soc, headroom, history) == expected
        assert fresh.target_kw(t, soc, headroom, history) == expected


# --- construction ---------------------------------------------------------------------------


def test_make_strategy_builds_it_from_config():
    cfg = load_config()
    books = {"LZ_HOUSTON": book_from(RAMP)}
    s = make_strategy(cfg.strategy, cfg.sim.tick_seconds, "forecast", books=books)
    assert isinstance(s, ForecastStrategy)
    assert s.horizon_ns == 12 * 3600 * 10**9  # config forecast.strategy.horizon_hours
    from src.sim.fleet import generate_batteries

    fleet = generate_batteries(cfg.fleet.size, cfg.battery, cfg.seed)
    assert s.duration_h == pytest.approx(fleet_duration_h([b.capacity_kwh for b in fleet],
                                                          [b.power_kw for b in fleet]))


def test_rejects_bad_parameters():
    with pytest.raises(ValueError):
        ForecastStrategy({}, duration_h=0.0)
    with pytest.raises(ValueError):
        ForecastStrategy({}, duration_h=1.0, efficiency=1.5)
    with pytest.raises(ValueError):
        fleet_duration_h([10.0], [0.0])
