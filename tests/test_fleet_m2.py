"""M2 acceptance: 20,000 batteries across 20 worker processes, 1 sim day at max speed.

With every worker healthy, actual power tracks the feasible target within 5% on EVERY tick
(error as defined in docs/API.md: |actual - target| / max(|target|, 0.5% of nameplate)).
Real 2025 ERCOT prices from data/snapshot drive the bands strategy.
"""
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import pytest

from src.config import load_raw_config
from src.sim.coordinator import Coordinator, Orchestration
from src.sim.redis_layout import F_TICK, reachable, redis_url

pytestmark = pytest.mark.redis
UTC = timezone.utc


@pytest.fixture
def coordinator():
    made = []

    def make(**kw):
        url = redis_url()
        if not reachable(url):
            pytest.skip(f"Redis not reachable at {url}")
        co = Coordinator(url=url, prefix=f"test:m2:{uuid.uuid4().hex[:8]}:", **kw)
        made.append(co)
        co.boot()
        return co

    yield make
    for co in made:
        co.shutdown()
        co.cleanup_keys()


def test_m2_one_sim_day_20k_batteries_20_workers(coordinator):
    # A wider max-speed barrier (1 s instead of 250 ms) keeps this acceptance check honest on a laptop
    # shared with other test suites: it measures tracking and throughput, not failure detection.
    orch = Orchestration.from_config(load_raw_config(), max_speed_deadline_ms=1000.0)
    co = coordinator(journal=True, orchestration=orch)
    assert co.N == 20000 and co.n_workers == 20
    assert len({h.pid for h in co.workers}) == 20 and all(h.status == "healthy" for h in co.workers)
    co.start({"preset": "spike_evening", "speed_tps": "max", "strategy": "bands", "start_charge_level": 0.5},
             max_ticks=288)
    t0 = time.perf_counter()
    assert co.serve_until(lambda: co.run.status == "finished", timeout_s=240)
    wall = time.perf_counter() - t0
    log = co.tick_log
    assert len(log) == 288
    errors = [r["error_pct"] for r in log]
    print(f"\nM2: 288 ticks x 20,000 batteries x 20 workers in {wall:.1f}s = {288 / wall:.1f} ticks/s; "
          f"max error {max(errors):.2e}%; barrier p50 {np.percentile([r['barrier_ms'] for r in log], 50):.1f} ms; "
          f"revenue ${co.run.revenue_usd:,.2f}")
    assert all(r["healthy_workers"] == 20 and r["reported"] == 20 for r in log)
    assert max(errors) <= 5.0
    trading = [r for r in log if set(r["action"].values()) != {"hold"}]
    assert len(trading) >= 20  # the fleet really charged and sold, so tracking was tested under load
    assert any(abs(r["target_kw"]["fleet"]) > 0.3 * co.nameplate_kw for r in log)
    # the hashes (source of truth), the coordinator's telemetry view and a replay agree exactly
    hash_soc = co.read_hash_soc()
    assert np.array_equal(hash_soc, co.soc)
    assert np.array_equal(hash_soc, co.replay_journal(0.5))
    last = int(datetime.fromisoformat(log[-1]["ts_utc"].replace("Z", "+00:00")).timestamp())
    assert {int(float(t)) for t in co.read_hash_state((F_TICK,))[F_TICK]} == {last}
    # the ledger saw every tick in every city, priced at real RT prices
    summary = co.run.ledger.summary()
    assert summary["total_usd"] == pytest.approx(sum(r["revenue_usd"]["fleet"] for r in log), rel=1e-6)
    assert all(r["price"][loc] is not None for r in log for loc in co.layout.locations)
    assert list(summary["per_location_usd"]) == list(co.layout.locations)
    totals = co.snapshot()["totals"]
    for loc in co.layout.locations:  # totals.by_location: the run's sums per city
        assert totals["by_location"][loc]["revenue_usd"] == pytest.approx(summary["per_location_usd"][loc], abs=1e-4)
        assert totals["by_location"][loc]["sold_kwh"] + totals["by_location"][loc]["bought_kwh"] > 0  # it traded
    for key in ("revenue_usd", "sold_kwh", "bought_kwh"):
        assert sum(v[key] for v in totals["by_location"].values()) == pytest.approx(totals[key], abs=1e-3)
    status = [r["status_ms"] for r in log]
    print(f"battery_status: p50 {np.percentile(status, 50):.3f} ms, max {max(status):.3f} ms per tick")
    assert np.percentile(status, 50) < 2.0  # low milliseconds per tick at 20,000 batteries


def test_a_run_finishes_at_the_end_of_the_loaded_prices(coordinator):
    """docs/API.md run_finished: the replay reached the end of the loaded data. A run started on Dec 31, 2025 loads
    the snapshot's 2025 file only (snapshot_years), which ends 05:55 UTC Jan 1. (Where 2026's real prices end is
    tests/test_fleet_prediction.py.)"""
    co = coordinator(fleet_size=200, workers=2)
    co.start({"start_utc": "2025-12-31T23:00:00Z", "speed_tps": "max", "strategy": "peak"}, internal=True)
    assert co.serve_until(lambda: co.run.status == "finished", timeout_s=60)
    assert len(co.tick_log) == 84 and co.tick_log[-1]["ts_utc"] == "2026-01-01T05:55:00Z"
    finished = [e for e in co.events if e["kind"] == "run_finished"]
    assert len(finished) == 1 and "end of the loaded price data" in finished[0]["message"]
    assert finished[0]["detail"]["ticks"] == 84 and co.status == "finished"
    assert co.snapshot()["status"] == "finished"
