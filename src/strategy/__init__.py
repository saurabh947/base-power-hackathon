"""Strategy implementations, chosen by name from config.yaml (strategy.name)."""

from __future__ import annotations

from src.config import StrategyConfig
from src.core.interfaces import Strategy
from src.strategy.bands import BandsStrategy
from src.strategy.peak import PeakStrategy

STRATEGY_NAMES = ("bands", "peak")


def make_strategy(cfg: StrategyConfig, tick_seconds: int, name: str | None = None) -> Strategy:
    """Build the configured strategy, or `name` if given. Unknown names raise ValueError."""
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
    raise ValueError(f"unknown strategy {name!r}; known: {', '.join(STRATEGY_NAMES)}")
