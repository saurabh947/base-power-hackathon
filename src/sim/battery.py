"""Vectorized battery physics (numpy).

The in-memory adapter, the worker processes and the in-process full-year
backtest all share this module. Every function takes arrays over batteries
(scalars broadcast) and returns new arrays. Nothing is mutated in place.

Everything is GRID-SIDE, measured at the AC meter (RUNLOG decision 1):
  - Discharging at P kW for dt hours removes P*dt/sqrt(eta) kWh from storage.
  - Charging at |P| kW adds |P|*dt*sqrt(eta) kWh to storage.
  - |P| <= power_kw, and SoC stays within [soc_min, soc_max].
  - A round trip returns eta of the grid energy.
Sign: + = discharge to grid (sell), - = charge from grid (buy).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray

SOC_MIN = 0.20
SOC_MAX = 1.00

Floats = NDArray[np.float64]


def headroom_kw(
    soc: ArrayLike,
    capacity_kwh: ArrayLike,
    power_kw: ArrayLike,
    efficiency: ArrayLike,
    available: ArrayLike,
    dt_h: float,
    soc_min: float = SOC_MIN,
    soc_max: float = SOC_MAX,
) -> tuple[Floats, Floats]:
    """Feasible grid-side kW for one tick, per battery, as (discharge, charge), both >= 0.

    discharge = min(power_kw, (soc - soc_min) * capacity * sqrt(eta) / dt)
    charge    = min(power_kw, (soc_max - soc) * capacity / (sqrt(eta) * dt))
    Unavailable batteries get 0 both ways.
    """
    soc = np.asarray(soc, dtype=np.float64)
    capacity = np.asarray(capacity_kwh, dtype=np.float64)
    power = np.asarray(power_kw, dtype=np.float64)
    root_eta = np.sqrt(np.asarray(efficiency, dtype=np.float64))
    available = np.asarray(available, dtype=bool)
    discharge = np.minimum(power, (soc - soc_min) * capacity * root_eta / dt_h)
    charge = np.minimum(power, (soc_max - soc) * capacity / (root_eta * dt_h))
    discharge = np.where(available, np.maximum(discharge, 0.0), 0.0)
    charge = np.where(available, np.maximum(charge, 0.0), 0.0)
    return discharge, charge


def step(
    soc: ArrayLike,
    capacity_kwh: ArrayLike,
    power_kw: ArrayLike,
    efficiency: ArrayLike,
    available: ArrayLike,
    setpoint_kw: ArrayLike,
    dt_h: float,
    soc_min: float = SOC_MIN,
    soc_max: float = SOC_MAX,
) -> tuple[Floats, Floats]:
    """Advance one tick. Returns (new_soc, delivered_kw).

    delivered_kw is the grid-side setpoint clipped to
    [-charge_headroom, +discharge_headroom], so it is 0 for unavailable
    batteries. Energy moved this tick = delivered_kw * dt_h.
    Raises ValueError if any setpoint is not finite.
    """
    setpoint = np.asarray(setpoint_kw, dtype=np.float64)
    if not np.all(np.isfinite(setpoint)):
        raise ValueError("setpoint_kw must be finite")
    soc = np.asarray(soc, dtype=np.float64)
    capacity = np.asarray(capacity_kwh, dtype=np.float64)
    root_eta = np.sqrt(np.asarray(efficiency, dtype=np.float64))
    discharge, charge = headroom_kw(soc, capacity, power_kw, efficiency, available, dt_h, soc_min, soc_max)
    delivered = np.minimum(np.maximum(setpoint, -charge), discharge)
    stored_delta = np.where(delivered > 0, -delivered * dt_h / root_eta, -delivered * dt_h * root_eta)
    new_soc = soc + stored_delta / capacity
    # Driving a battery exactly to a bound can overshoot it by one ulp. Clamp
    # only in the direction of travel, so an out-of-range SoC never jumps.
    new_soc = np.where(delivered > 0, np.maximum(new_soc, soc_min), new_soc)
    new_soc = np.where(delivered < 0, np.minimum(new_soc, soc_max), new_soc)
    return new_soc, delivered


def grid_kwh_from_soc_change(
    soc_before: ArrayLike,
    soc_after: ArrayLike,
    capacity_kwh: ArrayLike,
    efficiency: ArrayLike,
) -> Floats:
    """Grid-side kWh (+ sold, - bought) implied by a SoC change. Inverse of step().

    This lets a caller meter delivered energy through BatteryAdapter.read() alone.
    """
    stored = (np.asarray(soc_after, dtype=np.float64) - np.asarray(soc_before, dtype=np.float64)) * np.asarray(
        capacity_kwh, dtype=np.float64
    )
    root_eta = np.sqrt(np.asarray(efficiency, dtype=np.float64))
    return np.where(stored < 0, -stored * root_eta, -stored / root_eta)


def fleet_soc(soc: ArrayLike, capacity_kwh: ArrayLike, available: ArrayLike) -> float:
    """Capacity-weighted mean SoC of available batteries (0.0 if none are available)."""
    available = np.asarray(available, dtype=bool)
    capacity = np.where(available, np.asarray(capacity_kwh, dtype=np.float64), 0.0)
    total = float(capacity.sum())
    if total <= 0.0:
        return 0.0
    return float((np.asarray(soc, dtype=np.float64) * capacity).sum() / total)
