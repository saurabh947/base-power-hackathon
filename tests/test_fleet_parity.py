"""Parity helper: N ticks with given per-location targets through the worker fleet give exactly
the per-battery SoC of the in-process vectorized loop (same physics, same allocation)."""
import uuid

import numpy as np
import pytest

from src.config import load_config
from src.sim.allocation import allocate
from src.sim.battery import headroom_kw, step
from src.sim.coordinator import run_fleet_targets
from src.sim.redis_layout import FleetLayout, reachable, redis_url

pytestmark = pytest.mark.redis


def in_process(layout: FleetLayout, targets: dict, soc0: float, dt_h: float) -> np.ndarray:
    soc = np.full(layout.size, soc0)
    avail = np.ones(layout.size, dtype=bool)
    n = max(len(v) for v in targets.values())
    for k in range(n):
        for li, loc in enumerate(layout.locations):
            sl = layout.location_indices(li)
            d, c = headroom_kw(soc[sl], layout.capacity_kwh[sl], layout.power_kw[sl], layout.efficiency[sl], avail[sl], dt_h)
            _, sp = allocate(targets[loc][k], d, c)
            soc[sl], _ = step(soc[sl], layout.capacity_kwh[sl], layout.power_kw[sl], layout.efficiency[sl],
                              avail[sl], sp, dt_h)
    return soc


def test_parity_helper_matches_the_in_process_loop():
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    cfg = load_config()
    layout = FleetLayout.build(cfg, size=2000, workers=4)
    nameplate = {loc: float(layout.power_kw[layout.location_indices(li)].sum()) for li, loc in enumerate(layout.locations)}
    # synthetic targets (test input, not prices): charge hard, hold, sell, over-ask beyond headroom, flip
    shape = [-1.0, -0.6, -0.6, 0.0, 0.3, 0.8, 2.0, 2.0, -0.2, 0.0, -3.0, -1.0] * 4
    targets = {"LZ_HOUSTON": [x * nameplate["LZ_HOUSTON"] for x in shape],
               "LZ_NORTH": [-x * 0.5 * nameplate["LZ_NORTH"] for x in shape]}
    result = run_fleet_targets(targets, soc_init=0.55, fleet_size=2000, workers=4, url=url,
                               prefix=f"test:parity:{uuid.uuid4().hex[:8]}:")
    assert result["ticks"] == len(shape)
    assert result["battery_ids"][:2] == ["LZ_HOUSTON-000000", "LZ_HOUSTON-000001"]
    expected = in_process(layout, targets, 0.55, cfg.sim.dt_hours)
    assert np.array_equal(result["soc"], expected)
    assert np.array_equal(result["telemetry_soc"], expected)
    for tgt, act in zip(result["target_kw"], result["actual_kw"]):
        assert act["fleet"] == pytest.approx(tgt["fleet"], abs=1e-3)
