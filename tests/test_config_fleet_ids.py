"""Unit tests for config loading, the strategy registry, fleet generation and command ids."""

import copy
import uuid
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest
import yaml

from src.config import DEFAULT_CONFIG_PATH, ConfigError, load_config, parse_config
from src.core.ids import make_command_id
from src.sim.fleet import generate_batteries
from src.strategy import make_strategy
from src.strategy.bands import BandsStrategy
from src.strategy.peak import PeakStrategy

UTC = timezone.utc


@pytest.fixture(scope="module")
def raw():
    with DEFAULT_CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


# --- config -------------------------------------------------------------------------


def test_repo_config_holds_the_agreed_values():
    cfg = load_config()
    assert cfg.seed == 42
    assert (cfg.sim.tick_seconds, cfg.sim.decision_lag_ticks) == (300, 3)
    assert cfg.sim.dt_hours == pytest.approx(5 / 60) and cfg.sim.ticks_per_day == 288
    assert (cfg.fleet.size, cfg.fleet.workers) == (20000, 20)
    # the Texas Triangle by metro population, in this order (contiguous id blocks, docs/API.md)
    assert list(cfg.fleet.locations.items()) == [("LZ_HOUSTON", 0.35), ("LZ_NORTH", 0.40), ("LZ_CPS", 0.13),
                                                 ("LZ_AEN", 0.12)]
    from src.sim.fleet import location_counts

    assert location_counts(cfg.fleet.size, cfg.fleet.locations) == {"LZ_HOUSTON": 7000, "LZ_NORTH": 8000,
                                                                    "LZ_CPS": 2600, "LZ_AEN": 2400}
    b = cfg.battery
    assert b.power_kw == (5.0, 11.5) and b.capacity_kwh == (10.0, 20.0) and b.cap_power_at_capacity
    assert b.efficiency == 0.90 and (b.soc_min, b.soc_max) == (0.20, 1.00)
    assert (b.soc_init, b.demo_soc_init) == (0.5, 0.9)
    s = cfg.strategy
    assert s.name == "bands"
    assert (s.bands.low_percentile, s.bands.high_percentile) == (25.0, 90.0)
    assert (s.bands.window_days, s.bands.min_samples) == (7, 288)
    assert s.peak.timezone == "America/Chicago"
    assert s.peak.charge_window == (time(10), time(15)) and s.peak.discharge_window == (time(18), time(21))
    o = cfg.orchestration
    assert (o.heartbeat_interval_s, o.heartbeat_ttl_s, o.deadline_miss_threshold) == (2.0, 6.0, 2)


@pytest.mark.parametrize("path, value, message", [
    (("fleet", "locations"), {"LZ_HOUSTON": 0.5, "LZ_NORTH": 0.4}, "sum to 1"),
    (("battery", "efficiency"), 1.2, "efficiency"),
    (("battery", "soc_init"), 0.1, "soc_init"),
    (("battery", "power_kw"), [11.5, 5.0], "low"),
    (("strategy", "bands", "high_percentile"), 20, "percentiles"),
    (("strategy", "peak", "charge_window"), ["15:00", "10:00"], "start"),
    (("strategy", "peak", "timezone"), "Mars/Olympus", "unknown zone"),
    (("sim", "tick_seconds"), 7, "divide"),
    (("orchestration", "heartbeat_ttl_s"), 1, "heartbeat"),
    (("fleet", "size"), "many", "integer"),
])
def test_invalid_values_are_rejected(raw, path, value, message):
    bad = copy.deepcopy(raw)
    node = bad
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ConfigError, match=message):
        parse_config(bad)


def test_missing_key_names_the_key(raw):
    bad = copy.deepcopy(raw)
    del bad["battery"]["efficiency"]
    with pytest.raises(ConfigError, match="battery.efficiency"):
        parse_config(bad)


def test_strategy_swaps_via_config(raw):
    cfg = load_config()
    bands = make_strategy(cfg.strategy, cfg.sim.tick_seconds)
    assert isinstance(bands, BandsStrategy)
    assert (bands.window_ticks, bands.min_samples) == (2016, 288)
    swapped = copy.deepcopy(raw)
    swapped["strategy"]["name"] = "peak"
    peak_cfg = parse_config(swapped)
    assert isinstance(make_strategy(peak_cfg.strategy, peak_cfg.sim.tick_seconds), PeakStrategy)
    assert isinstance(make_strategy(cfg.strategy, 300, name="peak"), PeakStrategy)
    with pytest.raises(ValueError, match="unknown strategy"):
        make_strategy(cfg.strategy, 300, name="solver")


# --- fleet --------------------------------------------------------------------------


def test_fleet_specs_are_seeded_within_ranges_and_capped():
    cfg = load_config()
    fleet = generate_batteries(cfg.fleet.size, cfg.battery, cfg.seed)
    assert len(fleet) == 20000 and len({b.battery_id for b in fleet}) == 20000
    assert fleet[0].battery_id == "b00000" and fleet[-1].battery_id == "b19999"
    for b in fleet:
        assert 5.0 <= b.power_kw <= 11.5 and 10.0 <= b.capacity_kwh <= 20.0
        assert b.power_kw <= b.capacity_kwh  # at most 1C
        assert (b.soc, b.efficiency, b.available) == (0.5, 0.9, True)
    assert any(b.power_kw == b.capacity_kwh for b in fleet)  # the cap does bind for some
    assert generate_batteries(50, cfg.battery, cfg.seed) == fleet[:50]  # prefix-stable
    assert generate_batteries(50, cfg.battery, cfg.seed + 1) != fleet[:50]


def test_fleet_soc_override_is_validated():
    cfg = load_config()
    assert generate_batteries(2, cfg.battery, cfg.seed, soc_init=cfg.battery.demo_soc_init)[1].soc == 0.9
    with pytest.raises(ValueError):
        generate_batteries(2, cfg.battery, cfg.seed, soc_init=0.1)


# --- command ids --------------------------------------------------------------------


T = datetime(2025, 5, 16, 5, 0, tzinfo=UTC)


def test_command_id_is_the_pinned_uuid5():
    # Pinned so the formula can never drift silently (replays would stop deduping).
    assert make_command_id("run-1", "b00000", T) == "a4749707-584a-5ee0-948f-1f0513f27401"
    assert uuid.UUID(make_command_id("run-1", "b00000", T)).version == 5


def test_command_id_depends_on_run_battery_and_instant_only():
    base = make_command_id("run-1", "b00000", T)
    assert make_command_id("run-1", "b00000", T) == base
    assert make_command_id("run-1", "b00000", T.astimezone(ZoneInfo("America/Chicago"))) == base
    assert make_command_id(uuid.UUID(int=1), "b00000", T) != base
    assert make_command_id("run-2", "b00000", T) != base
    assert make_command_id("run-1", "b00001", T) != base
    assert make_command_id("run-1", "b00000", T.replace(minute=5)) != base
    with pytest.raises(ValueError):
        make_command_id("run-1", "b00000", T.replace(tzinfo=None))
