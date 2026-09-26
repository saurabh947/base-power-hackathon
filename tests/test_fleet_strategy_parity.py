"""The fleet's strategy path (decision lag, published tick, trailing history) decides exactly like
the frozen M0 skeleton on the M0 day: same real prices, same Strategy, so the same action every tick.
Bands and peak return +H, -H or 0, so the sign of the raw target must match tick for tick."""
import uuid

import numpy as np
import pytest

from src.config import load_raw_config
from src.sim import m0_prices, skeleton
from src.sim.coordinator import Coordinator, Orchestration
from src.sim.redis_layout import reachable, redis_url

pytestmark = pytest.mark.redis


@pytest.mark.parametrize("strategy", ["bands", "peak"])
def test_fleet_decisions_match_the_skeleton_on_the_m0_day(strategy):
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    reference = skeleton.run(strategy_name=strategy)  # lag 3 from config, 1 battery, in-process
    co = Coordinator(url=url, prefix=f"test:sparity:{uuid.uuid4().hex[:8]}:", fleet_size=40, workers=2,
                     orchestration=Orchestration.from_config(load_raw_config(), max_speed_deadline_ms=1000.0))
    try:
        co.boot()
        co.start({"start_utc": m0_prices.DAY_START_UTC.isoformat(), "speed_tps": "max", "strategy": strategy,
                  "start_charge_level": 0.5}, max_ticks=len(reference.rows), internal=True)
        assert co.serve_until(lambda: co.run.status == "finished", timeout_s=120)
        log = co.tick_log
        assert len(log) == len(reference.rows) == 288
        fleet = np.sign([r["raw_kw"]["LZ_HOUSTON"] for r in log])
        single = np.sign([row.target_kw for row in reference.rows])
        assert np.array_equal(fleet, single)
        # the prices the ledger used and the prices the strategy saw are the skeleton's too
        assert [r["price"]["LZ_HOUSTON"] for r in log] == pytest.approx([row.price_usd_mwh for row in reference.rows])
        seen = [row.seen_price_usd_mwh for row in reference.rows]
        assert [r["decision_price"]["LZ_HOUSTON"] for r in log] == pytest.approx(seen)
        assert (fleet != 0).sum() > 10  # the day really traded
    finally:
        co.shutdown()
        co.cleanup_keys()
