"""Seeded battery specs from config (RUNLOG: per-battery random specs, seeded)."""

from __future__ import annotations

import math
from typing import Mapping

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


def location_counts(size: int, shares: Mapping[str, float]) -> dict[str, int]:
    """Batteries per location: floor(size * share), remainder by largest fraction (ties: config order)."""
    if size < 0:
        raise ValueError("size must be >= 0")
    exact = {loc: size * float(share) for loc, share in shares.items()}
    counts = {loc: int(math.floor(x)) for loc, x in exact.items()}
    order = sorted(shares, key=lambda loc: -(exact[loc] - counts[loc]))  # stable: config order breaks ties
    for loc in order[: size - sum(counts.values())]:
        counts[loc] += 1
    return counts


def assign_locations(batteries: list[BatteryState], shares: Mapping[str, float]) -> dict[str, list[BatteryState]]:
    """THE fleet split, shared by the live fleet and the backtest (parity depends on it).

    Locations take consecutive blocks of the battery list in config order: with
    20,000 batteries and the Texas Triangle shares (0.35 / 0.40 / 0.13 / 0.12),
    LZ_HOUSTON gets b00000-b06999, LZ_NORTH b07000-b14999, LZ_CPS b15000-b17599
    and LZ_AEN b17600-b19999.
    """
    out, first = {}, 0
    for loc, count in location_counts(len(batteries), shares).items():
        out[loc] = batteries[first:first + count]
        first += count
    return out
