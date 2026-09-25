"""Fleet target -> per-battery setpoints (spec section 6, RUNLOG strategy inputs).

The Strategy sees a symmetric headroom H (fleet_headroom_kw) and returns a
target in [-H, +H]. allocate() clips that target to the feasible headroom in
the commanded direction, then splits it in proportion to each battery's
feasible headroom in that direction. Unavailable batteries have 0 headroom,
so they get 0.
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import ArrayLike, NDArray


def fleet_headroom_kw(power_kw: ArrayLike, available: ArrayLike) -> float:
    """Symmetric H handed to the Strategy: sum of power_kw over available batteries."""
    return float(np.where(np.asarray(available, dtype=bool), np.asarray(power_kw, dtype=np.float64), 0.0).sum())


def allocate(
    target_kw: float,
    discharge_headroom_kw: ArrayLike,
    charge_headroom_kw: ArrayLike,
) -> tuple[float, NDArray[np.float64]]:
    """Returns (feasible_target_kw, setpoints_kw).

    Each |setpoint| <= that battery's feasible headroom in the commanded
    direction, and the setpoints sum to the feasible target. Raises ValueError
    for a non-finite target.
    """
    if not math.isfinite(target_kw):
        raise ValueError(f"target_kw must be finite, got {target_kw!r}")
    discharge = np.asarray(discharge_headroom_kw, dtype=np.float64)
    charge = np.asarray(charge_headroom_kw, dtype=np.float64)
    if target_kw == 0.0:
        return 0.0, np.zeros_like(discharge)
    sign, headroom = (1.0, discharge) if target_kw > 0 else (-1.0, charge)
    total = float(headroom.sum())
    if total <= 0.0:
        return 0.0, np.zeros_like(headroom)
    magnitude = min(abs(target_kw), total)
    # ratio <= 1 and float multiplication is monotone, so no battery exceeds its headroom.
    setpoints = sign * headroom * (magnitude / total)
    return sign * magnitude, setpoints
