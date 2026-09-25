"""Load config.yaml into typed, frozen, validated dataclasses.

Usage:
    from src.config import load_config
    cfg = load_config()            # repo-root config.yaml
    cfg.battery.efficiency         # 0.9
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


class ConfigError(ValueError):
    """config.yaml is missing a key or holds an invalid value."""


@dataclass(frozen=True)
class SimConfig:
    tick_seconds: int
    decision_lag_ticks: int

    @property
    def dt_hours(self) -> float:
        return self.tick_seconds / 3600.0

    @property
    def ticks_per_day(self) -> int:
        return 86400 // self.tick_seconds


@dataclass(frozen=True)
class FleetConfig:
    size: int
    workers: int
    locations: Mapping[str, float]  # location -> fleet share, sums to 1


@dataclass(frozen=True)
class BatteryConfig:
    power_kw: tuple[float, float]  # uniform [low, high]
    capacity_kwh: tuple[float, float]  # uniform [low, high]
    cap_power_at_capacity: bool
    efficiency: float  # round-trip
    soc_min: float
    soc_max: float
    soc_init: float
    demo_soc_init: float


@dataclass(frozen=True)
class BandsConfig:
    low_percentile: float
    high_percentile: float
    window_days: int
    min_samples: int


@dataclass(frozen=True)
class PeakConfig:
    timezone: str
    charge_window: tuple[time, time]  # half-open [start, end), local time
    discharge_window: tuple[time, time]


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    bands: BandsConfig
    peak: PeakConfig


@dataclass(frozen=True)
class OrchestrationConfig:
    heartbeat_interval_s: float
    heartbeat_ttl_s: float
    deadline_miss_threshold: int


@dataclass(frozen=True)
class Config:
    seed: int
    sim: SimConfig
    fleet: FleetConfig
    battery: BatteryConfig
    strategy: StrategyConfig
    orchestration: OrchestrationConfig


def load_config(path: str | Path | None = None) -> Config:
    """Read and validate a config file (default: repo-root config.yaml)."""
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with p.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return parse_config(raw)


def load_raw_config(path: str | Path | None = None) -> dict[str, Any]:
    """The raw YAML mapping, for area sections outside the typed core.

    presets, data, api, backtest and forecast each read their own section from
    here, so adding an area never touches the dataclasses above.
    """
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with p.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return dict(raw) if isinstance(raw, Mapping) else {}


def parse_config(raw: Any) -> Config:
    """Validate a parsed YAML mapping. Raises ConfigError naming the bad key."""
    if not isinstance(raw, Mapping):
        raise ConfigError("config root must be a mapping")

    sim_raw = _section(raw, "sim")
    sim = SimConfig(
        tick_seconds=_int(sim_raw, "sim.tick_seconds", "tick_seconds", minimum=1),
        decision_lag_ticks=_int(sim_raw, "sim.decision_lag_ticks", "decision_lag_ticks", minimum=0),
    )
    if 86400 % sim.tick_seconds:
        raise ConfigError("sim.tick_seconds must divide a day (86400 s)")

    fleet_raw = _section(raw, "fleet")
    locations_raw = _get(fleet_raw, "fleet.locations", "locations")
    if not isinstance(locations_raw, Mapping) or not locations_raw:
        raise ConfigError("fleet.locations must be a non-empty mapping of location -> share")
    locations = {str(k): _positive_float(v, f"fleet.locations.{k}") for k, v in locations_raw.items()}
    if not math.isclose(sum(locations.values()), 1.0, abs_tol=1e-9):
        raise ConfigError(f"fleet.locations shares must sum to 1, got {sum(locations.values())}")
    fleet = FleetConfig(
        size=_int(fleet_raw, "fleet.size", "size", minimum=1),
        workers=_int(fleet_raw, "fleet.workers", "workers", minimum=1),
        locations=locations,
    )

    bat_raw = _section(raw, "battery")
    soc_lo, soc_hi = _range(bat_raw, "battery.soc_bounds", "soc_bounds")
    if not (0.0 <= soc_lo < soc_hi <= 1.0):
        raise ConfigError("battery.soc_bounds must satisfy 0 <= low < high <= 1")
    battery = BatteryConfig(
        power_kw=_positive_range(bat_raw, "battery.power_kw", "power_kw"),
        capacity_kwh=_positive_range(bat_raw, "battery.capacity_kwh", "capacity_kwh"),
        cap_power_at_capacity=_bool(bat_raw, "battery.cap_power_at_capacity", "cap_power_at_capacity"),
        efficiency=_float(bat_raw, "battery.efficiency", "efficiency"),
        soc_min=soc_lo,
        soc_max=soc_hi,
        soc_init=_float(bat_raw, "battery.soc_init", "soc_init"),
        demo_soc_init=_float(bat_raw, "battery.demo_soc_init", "demo_soc_init"),
    )
    if not (0.0 < battery.efficiency <= 1.0):
        raise ConfigError("battery.efficiency must be in (0, 1]")
    for key in ("soc_init", "demo_soc_init"):
        if not (soc_lo <= getattr(battery, key) <= soc_hi):
            raise ConfigError(f"battery.{key} must lie within battery.soc_bounds")

    strat_raw = _section(raw, "strategy")
    bands_raw = _section(strat_raw, "bands", prefix="strategy.")
    bands = BandsConfig(
        low_percentile=_float(bands_raw, "strategy.bands.low_percentile", "low_percentile"),
        high_percentile=_float(bands_raw, "strategy.bands.high_percentile", "high_percentile"),
        window_days=_int(bands_raw, "strategy.bands.window_days", "window_days", minimum=1),
        min_samples=_int(bands_raw, "strategy.bands.min_samples", "min_samples", minimum=1),
    )
    if not (0.0 <= bands.low_percentile < bands.high_percentile <= 100.0):
        raise ConfigError("strategy.bands percentiles must satisfy 0 <= low < high <= 100")
    peak_raw = _section(strat_raw, "peak", prefix="strategy.")
    tz_name = str(_get(peak_raw, "strategy.peak.timezone", "timezone"))
    try:
        ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError
        raise ConfigError(f"strategy.peak.timezone: unknown zone {tz_name!r}") from exc
    peak = PeakConfig(
        timezone=tz_name,
        charge_window=_window(peak_raw, "strategy.peak.charge_window", "charge_window"),
        discharge_window=_window(peak_raw, "strategy.peak.discharge_window", "discharge_window"),
    )
    name = _get(strat_raw, "strategy.name", "name")
    if not isinstance(name, str) or not name:
        raise ConfigError("strategy.name must be a non-empty string")
    strategy = StrategyConfig(name=name, bands=bands, peak=peak)

    orch_raw = _section(raw, "orchestration")
    orchestration = OrchestrationConfig(
        heartbeat_interval_s=_float(orch_raw, "orchestration.heartbeat_interval_s", "heartbeat_interval_s"),
        heartbeat_ttl_s=_float(orch_raw, "orchestration.heartbeat_ttl_s", "heartbeat_ttl_s"),
        deadline_miss_threshold=_int(
            orch_raw, "orchestration.deadline_miss_threshold", "deadline_miss_threshold", minimum=1
        ),
    )
    if not (0.0 < orchestration.heartbeat_interval_s < orchestration.heartbeat_ttl_s):
        raise ConfigError("orchestration: need 0 < heartbeat_interval_s < heartbeat_ttl_s")

    return Config(
        seed=_int(raw, "seed", "seed", minimum=0),
        sim=sim,
        fleet=fleet,
        battery=battery,
        strategy=strategy,
        orchestration=orchestration,
    )


# --- small typed getters -------------------------------------------------------


def _get(section: Mapping[str, Any], path: str, key: str) -> Any:
    if key not in section:
        raise ConfigError(f"missing config key: {path}")
    return section[key]


def _section(raw: Mapping[str, Any], key: str, prefix: str = "") -> Mapping[str, Any]:
    value = _get(raw, prefix + key, key)
    if not isinstance(value, Mapping):
        raise ConfigError(f"{prefix}{key} must be a mapping")
    return value


def _int(section: Mapping[str, Any], path: str, key: str, minimum: int) -> int:
    value = _get(section, path, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path} must be an integer, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{path} must be >= {minimum}, got {value}")
    return value


def _float(section: Mapping[str, Any], path: str, key: str) -> float:
    return _as_float(_get(section, path, key), path)


def _as_float(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{path} must be a finite number, got {value!r}")
    return float(value)


def _positive_float(value: Any, path: str) -> float:
    out = _as_float(value, path)
    if out <= 0:
        raise ConfigError(f"{path} must be > 0, got {out}")
    return out


def _bool(section: Mapping[str, Any], path: str, key: str) -> bool:
    value = _get(section, path, key)
    if not isinstance(value, bool):
        raise ConfigError(f"{path} must be true or false, got {value!r}")
    return value


def _range(section: Mapping[str, Any], path: str, key: str) -> tuple[float, float]:
    value = _get(section, path, key)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(f"{path} must be a [low, high] pair")
    lo, hi = _as_float(value[0], path), _as_float(value[1], path)
    if lo > hi:
        raise ConfigError(f"{path}: low {lo} > high {hi}")
    return lo, hi


def _positive_range(section: Mapping[str, Any], path: str, key: str) -> tuple[float, float]:
    lo, hi = _range(section, path, key)
    if lo <= 0:
        raise ConfigError(f"{path} must be > 0")
    return lo, hi


def _window(section: Mapping[str, Any], path: str, key: str) -> tuple[time, time]:
    value = _get(section, path, key)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(f'{path} must be a ["HH:MM", "HH:MM"] pair')
    try:
        start, end = (time.fromisoformat(str(v)) for v in value)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not start < end:
        raise ConfigError(f"{path}: start must be before end (windows may not wrap midnight)")
    return start, end
