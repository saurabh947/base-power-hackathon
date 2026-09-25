"""What a Strategy may know at decision time (RUNLOG decision 5, "no peeking").

The strategy's tick is stamped with the DECISION time, so clock-based rules
such as the 18:00-21:00 peak window stay on the clock. Its price is the latest
*published* one: decision_lag_ticks back, one 15-min settlement interval.
"""
from __future__ import annotations

from src.core.models import PriceTick


def decision_tick(now: PriceTick, published: PriceTick) -> PriceTick:
    """Decision-time view: `now`'s timestamp and location, `published`'s price and context."""
    return PriceTick(now.ts, now.location, published.price_usd_mwh, published.demand_mw, published.fuel_mix)
