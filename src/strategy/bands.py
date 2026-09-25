"""Strategy v1: percentile bands (spec section 8, built exactly as specced).

Pinned details (RUNLOG):
  - Window: the trailing 7 days (window_ticks = 2016 five-minute ticks) of
    price_history, which already excludes the current tick.
  - Hold (0) while the window has fewer than min_samples (288) prices.
  - Charge (-H) if price <= p25. Otherwise discharge (+H) if price >= p90
    and price > 0. Otherwise hold. Charge is checked first, so it wins the
    tie when p25 == p90.
  - Percentiles use numpy's default 'linear' method.
  - The SoC floor is enforced by the caller: it clips the target to feasible
    headroom.
"""

from __future__ import annotations

import math

import numpy as np

from src.core.models import PriceTick


class BandsStrategy:
    def __init__(
        self,
        low_percentile: float = 25.0,
        high_percentile: float = 90.0,
        window_ticks: int = 7 * 288,
        min_samples: int = 288,
    ) -> None:
        if not (0.0 <= low_percentile < high_percentile <= 100.0):
            raise ValueError("need 0 <= low_percentile < high_percentile <= 100")
        if not (1 <= min_samples <= window_ticks):
            raise ValueError("need 1 <= min_samples <= window_ticks")
        self.low_percentile = float(low_percentile)
        self.high_percentile = float(high_percentile)
        self.window_ticks = int(window_ticks)
        self.min_samples = int(min_samples)

    def target_kw(self, tick: PriceTick,
                  fleet_soc: float,
                  fleet_headroom_kw: float,
                  price_history: list[float]) -> float:
        headroom = float(fleet_headroom_kw)
        price = float(tick.price_usd_mwh)
        if not (math.isfinite(headroom) and headroom > 0.0) or not math.isfinite(price):
            return 0.0
        n = len(price_history)
        if n < self.min_samples:
            return 0.0
        window = np.asarray(price_history[max(0, n - self.window_ticks):], dtype=np.float64)
        p_low, p_high = np.percentile(window, (self.low_percentile, self.high_percentile))
        if price <= p_low:
            return -headroom
        if price >= p_high and price > 0.0:
            return headroom
        return 0.0

    def __repr__(self) -> str:
        return (f"BandsStrategy(p{self.low_percentile:g}/p{self.high_percentile:g}, "
                f"window={self.window_ticks} ticks, min_samples={self.min_samples})")
