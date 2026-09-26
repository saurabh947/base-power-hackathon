"""Parity with the backtest (RUNLOG decision 4): one real sim day through the worker fleet (separate OS processes,
Redis) and through src.backtest's vectorized replay give bit-identical results: per-battery final SoC, per-tick
delivered kWh and feasible target, and the Ledger summary. Four cities (the Texas Triangle), 20 workers.

Same fleet split (src.sim.fleet.assign_locations), same strategy construction (make_strategy, no
extra context), same decision lag and history (warm-up = window_days before the start), same metering
(grid_kwh_from_soc_change) and the same ledger order (per tick, locations in config order).

Worker 17 holds batteries in two cities (at 20,000: 600 San Antonio + 400 Austin). On 2025-02-19 the two cities'
prices part ways (Austin tops $950/MWh, San Antonio $390), so the simple rule charges one while it sells the
other: worker 17's single command batch carries opposite setpoints, and still every battery matches.
"""
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.backtest.engine import build_fleets, load_ticks
from src.backtest.runner import replay
from src.config import load_config
from src.sim.coordinator import price_feed, run_fleet_replay, snapshot_years
from src.sim.redis_layout import FleetLayout, reachable, redis_url

pytestmark = pytest.mark.redis
UTC = timezone.utc
WORKERS = 20
SPLIT_WORKER = 17  # the controller whose cohort straddles San Antonio and Austin


def backtest_day(name: str, start_utc: str, ticks: int, soc0: float, size: int, *, fast: bool):
    cfg = load_config()
    start = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    end = start + timedelta(seconds=ticks * cfg.sim.tick_seconds)
    warmup_start = start - timedelta(days=cfg.strategy.bands.window_days)
    feed = price_feed(snapshot_years(start), cfg.fleet.locations)  # the fleet's default source: data/snapshot
    series = load_ticks(feed, list(cfg.fleet.locations), start, end, warmup_start)
    fleets = build_fleets(size, cfg.fleet.locations, cfg.battery, cfg.seed, soc_init=soc0)
    return replay(name, cfg, series, fleets, lag=cfg.sim.decision_lag_ticks, fast=fast), fleets


@pytest.mark.parametrize("name, start_utc, soc0, size", [
    ("bands", "2025-05-16T05:00:00Z", 0.5, 2000),     # the M0 day, local midnight
    ("peak", "2025-05-16T21:00:00Z", 0.9, 2000),      # the demo preset: spike evening, pre-charged
    ("bands", "2025-01-01T06:00:00Z", 0.5, 2000),     # warm-up reaches back into 2024
    ("bands", "2025-02-19T06:00:00Z", 0.5, 20000),    # worker 17's day, the full fleet: San Antonio and Austin diverge
])
def test_one_sim_day_matches_the_backtest_bit_for_bit(name, start_utc, soc0, size):
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    cfg = load_config()
    fleet = run_fleet_replay(name, start_utc=start_utc, ticks=288, soc_init=soc0, fleet_size=size, workers=WORKERS,
                             url=url, prefix=f"test:btparity:{uuid.uuid4().hex[:8]}:")
    assert fleet["ticks"] == 288 and fleet["strategy_effective"] == name
    assert list(fleet["locations"]) == list(cfg.fleet.locations)  # all four cities replayed
    for fast in (False, True):  # the per-tick seam path and the backtest's bands fast path
        bt, fleets = backtest_day(name, start_utc, 288, soc0, size, fast=fast)
        assert list(bt.runs) == list(cfg.fleet.locations)
        for loc, run in bt.runs.items():
            ours = fleet["locations"][loc]
            assert ours["battery_ids"] == list(fleets[loc].battery_ids)
            assert np.array_equal(ours["target_kw"], run.target_kw), loc
            assert np.array_equal(ours["feasible_kw"], run.feasible_kw), loc
            assert np.array_equal(ours["delivered_kwh"], run.delivered_kwh), loc
            assert np.array_equal(ours["soc_final"], run.soc_final), loc
        assert fleet["summary"] == bt.summary
    for loc in cfg.fleet.locations:  # every city really traded (kWh), so parity was tested under load everywhere
        assert np.abs(fleet["locations"][loc]["delivered_kwh"]).sum() > 1.0, loc
    # the controller that straddles two cities: its batteries match battery for battery
    owners = FleetLayout.build(cfg, size, WORKERS).default_owners()
    cohort = np.flatnonzero(owners == SPLIT_WORKER)
    split = FleetLayout.build(cfg, size, WORKERS).counts_by_location(cohort)
    assert list(split) == ["LZ_CPS", "LZ_AEN"]
    assert np.array_equal(fleet["soc"][cohort], np.concatenate([bt.runs[loc].soc_final for loc in bt.runs])[cohort])
    if start_utc.startswith("2025-02-19"):
        assert split == {"LZ_CPS": 600, "LZ_AEN": 400}
        cps, aen = fleet["locations"]["LZ_CPS"], fleet["locations"]["LZ_AEN"]
        opposite = int(np.sum(cps["target_kw"] * aen["target_kw"] < 0))  # buy one city, sell the other
        moving = [np.abs(x["feasible_kw"]) > 1e-6 for x in (cps, aen)]
        apart = int(np.sum((np.sign(cps["feasible_kw"]) != np.sign(aen["feasible_kw"])) & (moving[0] | moving[1])))
        assert opposite >= 10 and apart >= 50  # worker 17's two halves did different things, tick after tick
        print(f"\nworker 17 ({split}): opposite decisions in its two cities on {opposite} of 288 ticks, "
              f"different actions on {apart}")
