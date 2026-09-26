"""Full-year x full-fleet speed and identity (spec M4: under 10 minutes; target under 60 s per strategy).

Behind a marker: runs only with BACKTEST_SPEED=1 (make backtest-speed), because it replays the
whole of 2025 for 20,000 batteries several times (about 1-2 minutes). It prints the measured
numbers. Real prices from the committed snapshot.
"""
from __future__ import annotations

import os
import time

import numpy as np
import pytest

from src.backtest.runner import build_context, replay, run_strategy

speed = pytest.mark.skipif(os.environ.get("BACKTEST_SPEED") != "1",
                           reason="full-year speed test: set BACKTEST_SPEED=1 (make backtest-speed)")
TARGET_S = 60.0  # per strategy, both decision lags
HARD_LIMIT_S = 600.0  # spec section 9


@pytest.fixture(scope="module")
def ctx():
    os.environ.setdefault("SERIES_BACKEND", "snapshot")
    t0 = time.perf_counter()
    c = build_context(2025)
    c.load_seconds = time.perf_counter() - t0
    return c


@speed
@pytest.mark.parametrize("name", ["bands", "peak"])
def test_full_year_full_fleet_under_target(ctx, name, capsys):
    t0 = time.perf_counter()
    record = run_strategy(name, ctx)
    seconds = time.perf_counter() - t0
    m = record["metrics"]
    with capsys.disabled():
        print(f"\n[speed] {name}: {sum(f.size for f in ctx.fleets.values()):,} batteries x "
              f"{m['ticks_replayed']:,} ticks x 2 locations; lag 3 replay {m['replay_seconds']:.1f}s, "
              f"lag 0 replay {m['lag0']['replay_seconds']:.1f}s, total {seconds:.1f}s (prices loaded in "
              f"{ctx.load_seconds:.1f}s)")
        print(f"[speed] {name}: ${m['per_battery_usd_year']:,.2f}/battery/yr, fleet ${m['fleet_usd_year']:,.0f}, "
              f"lag 0 ${m['lag0']['per_battery_usd_year']:,.2f}/battery")
    assert m["coverage"]["fraction"] == 1.0 and m["ticks_replayed"] == 105120
    assert m["checks"]["storage_balance_max_abs_kwh"] < 1e-6
    assert seconds < TARGET_S < HARD_LIMIT_S


@speed
def test_bands_fast_path_equals_the_seam_for_the_whole_year(ctx, capsys):
    t0 = time.perf_counter()
    fast = replay("bands", ctx.cfg, ctx.series, ctx.fleets, lag=3, fast=True)
    t1 = time.perf_counter()
    seam = replay("bands", ctx.cfg, ctx.series, ctx.fleets, lag=3, fast=False)
    t2 = time.perf_counter()
    with capsys.disabled():
        print(f"\n[speed] bands lag 3: fast path {t1 - t0:.1f}s, seam every tick {t2 - t1:.1f}s")
    assert fast.fast_path and not seam.fast_path
    assert fast.summary == seam.summary
    for loc in ctx.fleets:
        a, b = fast.runs[loc], seam.runs[loc]
        assert np.array_equal(a.target_kw, b.target_kw) and np.array_equal(a.delivered_kwh, b.delivered_kwh)
        assert np.array_equal(a.soc_final, b.soc_final)
