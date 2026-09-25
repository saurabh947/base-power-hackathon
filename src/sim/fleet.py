"""Seeded battery specs from config (RUNLOG: per-battery random specs, seeded)."""

from __future__ import annotations

import numpy as np

from src.config import BatteryConfig
from src.core.models import BatteryState


def generate_batteries(
    n: int, battery: BatteryConfig, seed: int, *, soc_init: float | None = None
) -> list[BatteryState]:
    """n available batteries with power and capacity drawn uniformly from the config ranges.

    Battery i always uses draws 2i and 2i+1 of the seeded stream, so its specs
    do not depend on n. b00000 in the 1-battery skeleton is b00000 of the full
    fleet. power_kw is capped at capacity_kwh (at most 1C) when configured.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    soc = battery.soc_init if soc_init is None else float(soc_init)
    if not (battery.soc_min <= soc <= battery.soc_max):
        raise ValueError(f"soc_init {soc} is outside [{battery.soc_min}, {battery.soc_max}]")
    draws = np.random.default_rng(seed).random((n, 2))
    (p_lo, p_hi), (c_lo, c_hi) = battery.power_kw, battery.capacity_kwh
    power = p_lo + (p_hi - p_lo) * draws[:, 0]
    capacity = c_lo + (c_hi - c_lo) * draws[:, 1]
    if battery.cap_power_at_capacity:
        power = np.minimum(power, capacity)
    return [
        BatteryState(f"b{i:05d}", float(capacity[i]), float(power[i]), soc, battery.efficiency, True)
        for i in range(n)
    ]
