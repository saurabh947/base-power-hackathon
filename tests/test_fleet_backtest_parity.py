"""Parity with the backtest (RUNLOG decision 4): one real sim day through the worker fleet (separate
OS processes, Redis) and through src.backtest's vectorized replay give bit-identical results:
per-battery final SoC, per-tick delivered kWh and feasible target, and the Ledger summary.

Same fleet split (src.sim.fleet.assign_locations), same strategy construction (make_strategy, no
extra context), same decision lag and history (warm-up = window_days before the start), same metering
(grid_kwh_from_soc_change) and the same ledger order (per tick, locations in config order).
"""
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.backtest.engine import build_fleets, load_ticks
from src.backtest.runner import replay
from src.config import load_config
from src.sim.coordinator import price_feed, run_fleet_replay, snapshot_years
from src.sim.redis_layout import reachable, redis_url

pytestmark = pytest.mark.redis
UTC = timezone.utc
SIZE, WORKERS = 2000, 4


def backtest_day(name: str, start_utc: str, ticks: int, soc0: float, *, fast: bool):
    cfg = load_config()
    start = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    end = start + timedelta(seconds=ticks * cfg.sim.tick_seconds)
    warmup_start = start - timedelta(days=cfg.strategy.bands.window_days)
    feed = price_feed(snapshot_years(start))  # the fleet's default source: real prices from data/snapshot
    series = load_ticks(feed, list(cfg.fleet.locations), start, end, warmup_start)
    fleets = build_fleets(SIZE, cfg.fleet.locations, cfg.battery, cfg.seed, soc_init=soc0)
    return replay(name, cfg, series, fleets, lag=cfg.sim.decision_lag_ticks, fast=fast), fleets


@pytest.mark.parametrize("name, start_utc, soc0", [
    ("bands", "2025-05-16T05:00:00Z", 0.5),          # the M0 day, local midnight
    ("peak", "2025-05-16T21:00:00Z", 0.9),           # the demo preset: spike evening, pre-charged
    ("bands", "2025-01-01T06:00:00Z", 0.5),          # warm-up reaches back into 2024
])
def test_one_sim_day_matches_the_backtest_bit_for_bit(name, start_utc, soc0):
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    fleet = run_fleet_replay(name, start_utc=start_utc, ticks=288, soc_init=soc0, fleet_size=SIZE, workers=WORKERS,
                             url=url, prefix=f"test:btparity:{uuid.uuid4().hex[:8]}:")
    assert fleet["ticks"] == 288 and fleet["strategy_effective"] == name
    for fast in (False, True):  # the per-tick seam path and the backtest's bands fast path
        bt, fleets = backtest_day(name, start_utc, 288, soc0, fast=fast)
        for loc, run in bt.runs.items():
            ours = fleet["locations"][loc]
            assert ours["battery_ids"] == list(fleets[loc].battery_ids)
            assert np.array_equal(ours["target_kw"], run.target_kw), loc
            assert np.array_equal(ours["feasible_kw"], run.feasible_kw), loc
            assert np.array_equal(ours["delivered_kwh"], run.delivered_kwh), loc
            assert np.array_equal(ours["soc_final"], run.soc_final), loc
        assert fleet["summary"] == bt.summary
    moved = sum(np.abs(fleet["locations"][loc]["delivered_kwh"]).sum() for loc in fleet["locations"])
    assert moved > 1000.0  # the day really traded (kWh), so parity was tested under load
