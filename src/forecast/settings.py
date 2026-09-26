"""The `forecast:` section of config.yaml, typed and validated.

Read through src.config.load_raw_config()["forecast"], so the typed core
config never changes. Two environment variables override the file, so the
Cloud Run job can switch without a rebuild:
  FORECAST_MODEL        timesfm-3.0 | timesfm-2.5
  FORECAST_WEIGHTS_DIR  where model weights live (the image bakes them into /app/models)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from src.config import REPO_ROOT, ConfigError, load_raw_config

SERIES = ("rt_price", "demand")
COVARIATES = ("none", "da")


@dataclass(frozen=True)
class SeriesSettings:
    """One forecast series: its grid, horizon, context and issue cadence."""

    name: str  # rt_price | demand
    step_minutes: int  # native resolution of the series
    horizon_steps: int  # points per forecast
    context_steps: int  # most recent points fed to the model
    every_minutes: int  # issue cadence (rolling origin)
    min_context_steps: int  # skip an issue time with less observed history
    covariates: str = "none"  # rt_price only: none | da

    @property
    def horizon_minutes(self) -> int:
        return self.horizon_steps * self.step_minutes


@dataclass(frozen=True)
class StrategySettings:
    """Knobs of the forecast rule (src/strategy/forecast.py)."""

    horizon_hours: float  # look-ahead window the decision price is ranked in
    max_age_hours: float  # an older forecast is ignored (hold)
    min_points: int  # hold if fewer forecast points remain in the window
    charge_margin: float  # charge only if price <= eta * margin * (k-th best sell forecast)
    reserve_spike_usd: float  # p90 above this ahead -> keep a reserve (0 disables)
    reserve_soc: float  # SoC kept back while a spike is possible
    series: str = "rt_price"


@dataclass(frozen=True)
class ForecastSettings:
    model: str
    weights_dir: Path
    download: bool
    device: str
    batch_size: int
    threads: int  # 0 = every CPU the container may use
    timezone: str
    locations: tuple[str, ...]
    demand_location: str
    da_publish_local: time  # DA results for day D are posted about this time on D-1
    cache_dir: Path
    rt_price: SeriesSettings
    demand: SeriesSettings
    strategy: StrategySettings

    def series(self, name: str) -> SeriesSettings:
        if name == "rt_price":
            return self.rt_price
        if name == "demand":
            return self.demand
        raise ValueError(f"unknown series {name!r}; known: {', '.join(SERIES)}")

    def with_overrides(self, **changes: Any) -> ForecastSettings:
        return replace(self, **changes)


def load_settings(raw: Mapping[str, Any] | None = None, *, env: Mapping[str, str] | None = None) -> ForecastSettings:
    """Parse the forecast section (default: repo config.yaml), then apply env overrides."""
    if raw is None:
        raw = load_raw_config().get("forecast")
    if not isinstance(raw, Mapping):
        raise ConfigError("config.yaml needs a `forecast:` mapping")
    env = os.environ if env is None else env

    from src.forecast.model import MODELS  # registry only; no torch import

    model = str(env.get("FORECAST_MODEL") or _get(raw, "model"))
    if model not in MODELS:
        raise ConfigError(f"forecast.model must be one of {', '.join(MODELS)}, got {model!r}")
    tz = str(raw.get("timezone", "America/Chicago"))
    try:
        ZoneInfo(tz)
    except Exception as exc:
        raise ConfigError(f"forecast.timezone: unknown zone {tz!r}") from exc
    try:
        da_publish = time.fromisoformat(str(raw.get("da_publish_local", "13:30")))
    except ValueError as exc:
        raise ConfigError(f"forecast.da_publish_local: {exc}") from exc
    locations = raw.get("locations", ["LZ_HOUSTON", "LZ_NORTH"])
    if not isinstance(locations, (list, tuple)) or not locations:
        raise ConfigError("forecast.locations must be a non-empty list")

    rt = _series("rt_price", _mapping(raw, "rt_price"))
    demand = _series("demand", _mapping(raw, "demand"))
    if demand.covariates != "none":
        raise ConfigError("forecast.demand.covariates: only rt_price takes covariates")

    strat_raw = _mapping(raw, "strategy")
    strategy = StrategySettings(
        horizon_hours=_num(strat_raw, "horizon_hours", "strategy", positive=True),
        max_age_hours=_num(strat_raw, "max_age_hours", "strategy", positive=True),
        min_points=_int(strat_raw, "min_points", "strategy", minimum=1),
        charge_margin=_num(strat_raw, "charge_margin", "strategy", positive=True),
        reserve_spike_usd=_num(strat_raw, "reserve_spike_usd", "strategy"),
        reserve_soc=_num(strat_raw, "reserve_soc", "strategy"),
    )
    if not 0.0 <= strategy.reserve_soc <= 1.0:
        raise ConfigError("forecast.strategy.reserve_soc must be within [0, 1]")

    return ForecastSettings(
        model=model,
        weights_dir=_path(env.get("FORECAST_WEIGHTS_DIR") or raw.get("weights_dir", "models")),
        download=bool(raw.get("download", True)),
        device=str(raw.get("device", "cpu")),
        batch_size=_int(raw, "batch_size", "", minimum=1),
        threads=_int(raw, "threads", "", minimum=0),
        timezone=tz,
        locations=tuple(str(x) for x in locations),
        demand_location=str(raw.get("demand_location", "ERCOT")),
        da_publish_local=da_publish,
        cache_dir=_path(raw.get("cache_dir", "data/cache/forecasts")),
        rt_price=rt,
        demand=demand,
        strategy=strategy,
    )


def _series(name: str, raw: Mapping[str, Any]) -> SeriesSettings:
    s = SeriesSettings(
        name=name,
        step_minutes=_int(raw, "step_minutes", name, minimum=1),
        horizon_steps=_int(raw, "horizon_steps", name, minimum=1),
        context_steps=_int(raw, "context_steps", name, minimum=32),
        every_minutes=_int(raw, "every_minutes", name, minimum=1),
        min_context_steps=_int(raw, "min_context_steps", name, minimum=1),
        covariates=str(raw.get("covariates", "none")),
    )
    if s.covariates not in COVARIATES:
        raise ConfigError(f"forecast.{name}.covariates must be one of {', '.join(COVARIATES)}")
    if s.every_minutes % s.step_minutes:
        raise ConfigError(f"forecast.{name}.every_minutes must be a multiple of step_minutes")
    if s.min_context_steps > s.context_steps:
        raise ConfigError(f"forecast.{name}.min_context_steps must be <= context_steps")
    return s


def _path(value: Any) -> Path:
    p = Path(str(value))
    return p if p.is_absolute() else REPO_ROOT / p


def _get(section: Mapping[str, Any], key: str, where: str = "") -> Any:
    if key not in section:
        raise ConfigError(f"missing config key: forecast.{where + '.' if where else ''}{key}")
    return section[key]


def _mapping(section: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = _get(section, key)
    if not isinstance(value, Mapping):
        raise ConfigError(f"forecast.{key} must be a mapping")
    return value


def _int(section: Mapping[str, Any], key: str, where: str, minimum: int) -> int:
    value = _get(section, key, where)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"forecast.{where + '.' if where else ''}{key} must be an integer >= {minimum}")
    return value


def _num(section: Mapping[str, Any], key: str, where: str, positive: bool = False) -> float:
    value = _get(section, key, where)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (positive and value <= 0):
        raise ConfigError(f"forecast.{where}.{key} must be a {'positive ' if positive else ''}number")
    return float(value)
