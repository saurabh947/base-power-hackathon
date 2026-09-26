"""Strategy 'forecast': a rank rule over cached TimesFM price forecasts. A rule, not a solver.

At decision time t the tick carries the decision clock and the last
*published* price p (decision_tick, RUNLOG decision 5). Then:

1. Look up the latest forecast for tick.location issued at or before t
   (src.forecast.provider.ForecastBook, precomputed by src.forecast.job).
   None, or older than max_age_hours: hold.
2. Window W = the forecast points not yet over at t, up to horizon_hours
   ahead (12 h). Each 15-min point is three 5-minute slots, valued at its
   p50 forecast.
3. k = the 5-minute slots at full power that move a whole battery's usable
   energy: k_sell empties it (SoC 1.0 -> 0.2), k_buy fills it. They come from
   the fleet's energy-to-power ratio (duration_h, passed to the constructor).
4. Sell (+H) if p > 0 and fewer than k_sell slots in W are forecast higher:
   now is among the k best slots ahead. Buy (-H) if fewer than k_buy slots in
   W are forecast lower, and p <= eta * charge_margin * (the price of the
   k_sell-th best slot), so the round trip pays for its losses. Else hold.
   An empty fleet never "sells" and a full one never "buys", so the action
   the UI shows is the one that moves energy.
5. Optional reserve (reserve_spike_usd > 0, off by default): while SoC is at
   or below reserve_soc, do not sell if a p90 forecast in W is above
   reserve_spike_usd and above p. Measured on 2025 windows it cost revenue.

Why 12 h and a full-battery k (docs/notes/forecast.md): on 152 real 2025
days, shorter windows earned more but cycled more. 12 h cycles about as often
as the peak rule (~0.9 a day), so it wins on price timing, not extra wear.

Pure: the forecasts are loaded once in the constructor, and target_kw keeps
no state between calls. price_history is not used.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Mapping

import numpy as np

from src.core.models import PriceTick
from src.forecast.provider import ForecastBook

NS_PER_MIN = 60 * 10**9
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SOC_EPS = 1e-6


class ForecastStrategy:
    def __init__(
        self,
        books: Mapping[str, ForecastBook],
        *,
        duration_h: float,
        efficiency: float = 0.90,
        soc_min: float = 0.20,
        soc_max: float = 1.00,
        tick_seconds: int = 300,
        horizon_hours: float = 12.0,
        max_age_hours: float = 6.0,
        min_points: int = 8,
        charge_margin: float = 1.0,
        reserve_spike_usd: float = 0.0,
        reserve_soc: float = 0.5,
    ) -> None:
        if not (math.isfinite(duration_h) and duration_h > 0):
            raise ValueError("duration_h (fleet kWh / fleet kW) must be > 0")
        if not (0.0 < efficiency <= 1.0) or not (0.0 <= soc_min < soc_max <= 1.0):
            raise ValueError("need 0 < efficiency <= 1 and 0 <= soc_min < soc_max <= 1")
        if not (horizon_hours > 0 and max_age_hours > 0 and tick_seconds > 0):
            raise ValueError("horizon_hours, max_age_hours and tick_seconds must be > 0")
        self.books = dict(books)
        self.duration_h = float(duration_h)
        self.efficiency = float(efficiency)
        self.soc_min, self.soc_max = float(soc_min), float(soc_max)
        self.tick_seconds = int(tick_seconds)
        self.horizon_ns = int(horizon_hours * 60) * NS_PER_MIN
        self.max_age_ns = int(max_age_hours * 60) * NS_PER_MIN
        self.min_points = int(min_points)
        self.charge_margin = float(charge_margin)
        self.reserve_spike_usd = float(reserve_spike_usd)
        self.reserve_soc = float(reserve_soc)
        dt_h, root_eta, usable = tick_seconds / 3600.0, math.sqrt(efficiency), soc_max - soc_min
        # grid-side hours at full power: discharge delivers usable*sqrt(eta), charging stores |P|*sqrt(eta)
        self.k_sell = max(1, math.ceil(usable * self.duration_h * root_eta / dt_h - 1e-9))
        self.k_buy = max(1, math.ceil(usable * self.duration_h / root_eta / dt_h - 1e-9))

    def target_kw(self, tick: PriceTick,
                  fleet_soc: float,
                  fleet_headroom_kw: float,
                  price_history: list[float]) -> float:
        headroom = float(fleet_headroom_kw)
        price = float(tick.price_usd_mwh)
        soc = float(fleet_soc)
        if not (math.isfinite(headroom) and headroom > 0.0) or not math.isfinite(price) or not math.isfinite(soc):
            return 0.0
        window = self._window(tick)
        if window is None:
            return 0.0
        p50, p90, slots = window
        if price > 0.0 and soc > self.soc_min + _SOC_EPS and slots * np.count_nonzero(p50 > price) < self.k_sell:
            if self.reserve_spike_usd > 0.0 and soc <= self.reserve_soc:
                spike = float(p90.max())
                if spike >= self.reserve_spike_usd and spike > price:
                    return 0.0  # keep the reserve for the spike the p90 warns about
            return headroom
        if (soc < self.soc_max - _SOC_EPS and slots * np.count_nonzero(p50 < price) < self.k_buy
                and price <= self.efficiency * self.charge_margin * self._sell_at(p50, slots)):
            return -headroom
        return 0.0

    def _sell_at(self, p50: np.ndarray, slots: int) -> float:
        """Price of the k_sell-th best slot ahead: what energy bought now should sell for."""
        n = len(p50)
        kth = n - min(n, max(1, math.ceil(self.k_sell / slots)))
        return float(np.partition(p50, kth)[kth])

    def _window(self, tick: PriceTick) -> tuple[np.ndarray, np.ndarray, int] | None:
        book = self.books.get(tick.location)
        if book is None or not len(book):
            return None
        if tick.ts.tzinfo is None or tick.ts.utcoffset() is None:
            raise ValueError(f"tick.ts must be timezone-aware, got {tick.ts!r}")
        now = (tick.ts - _EPOCH) // timedelta(microseconds=1) * 1000  # exact ns
        i = book.latest(now)
        if i is None or now - int(book.issued[i]) > self.max_age_ns:
            return None
        step_ns = book.step_minutes * NS_PER_MIN
        times = book.point_times(i)
        ahead = (times + step_ns > now) & (times < now + self.horizon_ns)
        if np.count_nonzero(ahead) < self.min_points:
            return None
        p50 = book.p50[i][ahead]
        finite = np.isfinite(p50)
        if np.count_nonzero(finite) < self.min_points:
            return None
        slots = max(1, book.step_minutes * 60 // self.tick_seconds)
        return p50[finite], book.p90[i][ahead][finite], slots

    def __repr__(self) -> str:
        return (f"ForecastStrategy({', '.join(sorted(self.books))}, duration {self.duration_h:.2f} h, "
                f"k_sell {self.k_sell}, k_buy {self.k_buy}, horizon {self.horizon_ns // (60 * NS_PER_MIN)} h)")


def fleet_duration_h(capacity_kwh, power_kw) -> float:
    """Fleet energy-to-power ratio: total capacity / total power (hours at full power, SoC 0 to 1)."""
    power = float(np.sum(power_kw))
    if power <= 0:
        raise ValueError("fleet power must be > 0")
    return float(np.sum(capacity_kwh)) / power


def make_forecast_strategy(
    tick_seconds: int = 300,
    *,
    books: Mapping[str, ForecastBook] | None = None,
    batteries=None,
    locations=None,
    price_source: str | None = None,
) -> ForecastStrategy:
    """ForecastStrategy from config.yaml: rule knobs from forecast.strategy, physics from battery.

    books: {location: ForecastBook}; default: loaded via src.forecast.provider for
    forecast.locations (Postgres when configured, else the parquet cache).
    batteries: the BatteryStates this instance steers (for duration_h); default:
    the configured fleet from src.sim.fleet.generate_batteries.
    price_source: where a year's real_until_utc is read for the virtual forecasts after it
    ('postgres' or 'snapshot'; default env PRICE_SOURCE, as the live fleet reads its prices).
    """
    from src.config import load_config
    from src.forecast.settings import load_settings
    from src.sim.fleet import generate_batteries

    cfg = load_config()
    settings = load_settings()
    if batteries is None:
        batteries = generate_batteries(cfg.fleet.size, cfg.battery, cfg.seed)
    duration = fleet_duration_h([b.capacity_kwh for b in batteries], [b.power_kw for b in batteries])
    if books is None:
        # virtual=True: after a year's real_until_utc the book holds hourly slices of the AI's prediction
        books = {loc: ForecastBook.load(loc, settings.strategy.series, virtual=True, price_source=price_source)
                 for loc in (locations or settings.locations)}
    st = settings.strategy
    return ForecastStrategy(
        books,
        duration_h=duration,
        efficiency=cfg.battery.efficiency,
        soc_min=cfg.battery.soc_min,
        soc_max=cfg.battery.soc_max,
        tick_seconds=tick_seconds,
        horizon_hours=st.horizon_hours,
        max_age_hours=st.max_age_hours,
        min_points=st.min_points,
        charge_margin=st.charge_margin,
        reserve_spike_usd=st.reserve_spike_usd,
        reserve_soc=st.reserve_soc,
    )
