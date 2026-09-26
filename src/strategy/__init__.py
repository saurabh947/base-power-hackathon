"""Strategy implementations, chosen by name from config.yaml (strategy.name)."""

from __future__ import annotations

from typing import Any

from src.config import StrategyConfig
from src.core.interfaces import Strategy
from src.strategy.bands import BandsStrategy
from src.strategy.peak import PeakStrategy

STRATEGY_NAMES = ("bands", "peak", "forecast")


def make_strategy(cfg: StrategyConfig, tick_seconds: int, name: str | None = None, **context: Any) -> Strategy:
    """Build the configured strategy, or `name` if given. Unknown names raise ValueError.

    `context` goes to the forecast strategy only (src/strategy/forecast.py):
    books={location: ForecastBook} and batteries=[BatteryState, ...] for the
    cohort this instance steers. Both default from config.yaml.
    """
    name = name or cfg.name
    if name == "bands":
        b = cfg.bands
        return BandsStrategy(
            low_percentile=b.low_percentile,
            high_percentile=b.high_percentile,
            window_ticks=b.window_days * 86400 // tick_seconds,
            min_samples=b.min_samples,
        )
    if name == "peak":
        p = cfg.peak
        return PeakStrategy(p.timezone, p.charge_window, p.discharge_window)
    if name == "forecast":
        from src.strategy.forecast import make_forecast_strategy  # lazy: reads the cached forecasts

        return make_forecast_strategy(tick_seconds, **context)
    raise ValueError(f"unknown strategy {name!r}; known: {', '.join(STRATEGY_NAMES)}")
