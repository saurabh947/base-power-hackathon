"""Control plane: settings validation, availability overrides and event wording. No services needed."""
import numpy as np
import pytest

from src.config import load_config, load_raw_config
from src.sim import control as ctl
from src.sim.redis_layout import FleetLayout


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def raw():
    return load_raw_config()


def test_default_settings_follow_docs_api(cfg, raw):
    s = ctl.default_settings(cfg, raw)
    assert s == {
        "preset": "spike_evening", "start_utc": "2025-05-16T21:00:00Z", "speed_tps": 2, "strategy": "forecast",
        "start_charge_level": 0.9, "fleet_size": 20000, "power_kw_range": [5.0, 11.5],
        "capacity_kwh_range": [10.0, 20.0], "locations": {"LZ_HOUSTON": 0.5, "LZ_NORTH": 0.5},
    }


def test_resolve_settings_takes_only_editable_keys(cfg, raw):
    s = ctl.resolve_settings({"preset": "summer_spike", "speed_tps": 5, "strategy": "peak", "start_charge_level": 0.5,
                              "fleet_size": 3, "start_utc": "2020-01-01T00:00:00Z"}, cfg, raw)
    assert (s["preset"], s["start_utc"], s["speed_tps"], s["strategy"], s["start_charge_level"]) == (
        "summer_spike", "2025-07-11T23:00:00Z", 5, "peak", 0.5)
    assert s["fleet_size"] == 20000  # ignored
    internal = ctl.resolve_settings({"start_utc": "2025-03-01T06:00:00Z"}, cfg, raw, allow_internal=True)
    assert internal["start_utc"] == "2025-03-01T06:00:00Z"


@pytest.mark.parametrize("partial, code", [
    ({"preset": "nope"}, "invalid_preset"),
    ({"speed_tps": 3}, "invalid_speed"),
    ({"speed_tps": "max"}, "invalid_speed"),
    ({"speed_tps": True}, "invalid_speed"),
    ({"strategy": "solver"}, "invalid_strategy"),
    ({"start_charge_level": 0.1}, "invalid_start_charge_level"),
    ({"start_charge_level": "full"}, "invalid_start_charge_level"),
])
def test_invalid_settings_are_400s(cfg, raw, partial, code):
    with pytest.raises(ctl.ControlError) as err:
        ctl.resolve_settings(partial, cfg, raw)
    assert err.value.status == 400 and err.value.code == code and err.value.message.endswith(".")


def test_max_speed_only_when_allowed():
    assert ctl.parse_speed("max", allow_max=True) == "max"
    assert ctl.parse_speed(10) == 10 and ctl.parse_speed(2.0) == 2


@pytest.fixture
def layout(cfg):
    return FleetLayout.build(cfg, size=2000, workers=4)


def test_location_fraction_is_seeded_and_monotone(cfg, layout):
    a = ctl.AvailabilityState(layout, cfg.seed)
    owners = layout.default_owners()
    d = a.apply({"scope": "location", "location": "LZ_HOUSTON", "offline_fraction": 0.3}, owners, 4)
    first = a.offline.copy()
    assert d["affected"] == 300 and a.summary()["by_location"]["LZ_HOUSTON"] == {"available": 700, "total": 1000}
    assert not a.offline[layout.location_indices("LZ_NORTH")].any()
    b = ctl.AvailabilityState(layout, cfg.seed)
    b.apply({"scope": "location", "location": "LZ_HOUSTON", "offline_fraction": 0.3}, owners, 4)
    assert np.array_equal(first, b.offline)  # the same fraction always picks the same batteries
    b.apply({"scope": "location", "location": "LZ_HOUSTON", "offline_fraction": 0.5}, owners, 4)
    assert (b.offline >= first).all() and b.offline.sum() == 500  # bigger fraction = superset
    d0 = b.apply({"scope": "location", "location": "LZ_HOUSTON", "offline_fraction": 0}, owners, 4)
    assert d0["affected"] == 500 and not b.offline.any()


def test_worker_battery_and_all_scopes(cfg, layout):
    a = ctl.AvailabilityState(layout, cfg.seed)
    owners = layout.default_owners()
    d = a.apply({"scope": "worker", "worker_id": 2, "available": False}, owners, 4)
    assert d["affected"] == 500 and a.summary()["offline_workers"] == [2] and a.summary()["available"] == 1500
    bid = layout.ids[int(np.flatnonzero(owners == 2)[0])]
    a.apply({"scope": "battery", "battery_id": bid, "available": True}, owners, 4)
    assert a.summary()["offline_workers"] == []  # not all of worker 2's batteries are offline any more
    a.apply({"scope": "battery", "battery_id": "b01003", "available": False}, owners, 4)
    d = a.apply({"scope": "all", "available": True}, owners, 4)
    assert d["affected"] == 500 and a.summary()["available"] == 2000


@pytest.mark.parametrize("req, status, code", [
    ({"scope": "location", "location": "LZ_WEST", "offline_fraction": 0.1}, 400, "invalid_location"),
    ({"scope": "location", "location": "LZ_NORTH", "offline_fraction": 1.5}, 400, "invalid_fraction"),
    ({"scope": "worker", "worker_id": 9, "available": False}, 404, "unknown_worker"),
    ({"scope": "worker", "worker_id": 1, "available": "no"}, 400, "invalid_available"),
    ({"scope": "battery", "battery_id": "b99999", "available": False}, 404, "unknown_battery"),
    ({"scope": "galaxy"}, 400, "invalid_scope"),
])
def test_bad_availability_requests(cfg, layout, req, status, code):
    a = ctl.AvailabilityState(layout, cfg.seed)
    with pytest.raises(ctl.ControlError) as err:
        a.apply(req, layout.default_owners(), 4)
    assert (err.value.status, err.value.code) == (status, code)
    assert not a.offline.any()


def test_plain_language_wording():
    assert ctl.speed_phrase(2) == "1 hour of market time every 6 seconds"
    assert ctl.speed_phrase(1) == "1 hour of market time every 12 seconds"
    assert ctl.speed_phrase(5) == "1 hour of market time every 2.4 seconds"
    assert ctl.price_phrase(2250.15) == "$2,250/MWh ($2.25 per kWh)"
    assert ctl.price_phrase(120) == "$120/MWh (12¢ per kWh)"
    assert ctl.local_clock(1747446300) == "May 16, 2025, 8:45 pm"
    assert ctl.action_message({"LZ_HOUSTON": "sell", "LZ_NORTH": "sell"}) == \
        "Batteries in both areas are now selling while prices are high."
    msg = ctl.action_message({"LZ_HOUSTON": "charge", "LZ_NORTH": "hold"})
    assert "Houston area" in msg and "Dallas–Fort Worth area" in msg
    e = ctl.make_event("worker_dead", "warn", "Controller 7 stopped responding.", sim_ts=1747429500)
    assert e["sim_utc"] == "2025-05-16T21:05:00Z" and e["wall_utc"].endswith("Z")
    with pytest.raises(ValueError):
        ctl.make_event("chaos", "info", "x")
    with pytest.raises(ValueError):
        ctl.make_event("run_started", "loud", "x")


START = 1747429200  # 2025-05-16T21:00:00Z, the demo preset
LOCS = ["LZ_HOUSTON", "LZ_NORTH"]


def _synthetic_books(first_issue_s: int, hours: int) -> dict:
    """Synthetic forecast books (test doubles, not real forecasts): hourly issues, 24 h of 15-min points."""
    from src.forecast.provider import ForecastBook

    issued = (first_issue_s + np.arange(hours, dtype=np.int64) * 3600) * 10**9
    shape = np.tile(np.linspace(20.0, 80.0, 96, dtype=np.float32), (hours, 1))
    return {loc: ForecastBook(loc, "rt_price", "synthetic", 15, issued, issued.copy(), shape - 5, shape, shape + 5)
            for loc in LOCS}


@pytest.fixture
def strategy_factory(monkeypatch):
    """make_strategy with 'forecast' swapped for a stand-in, and the coordinator's strategy cache emptied."""
    import src.strategy
    from src.sim import coordinator

    monkeypatch.setattr(coordinator, "_STRATEGY_CACHE", {})
    real = src.strategy.make_strategy
    box = {}

    def fake(c, tick_seconds, name=None, **context):
        if name == "forecast":
            return box["forecast"]()
        return real(c, tick_seconds, name, **context)

    monkeypatch.setattr(src.strategy, "make_strategy", fake)
    return box


def test_forecast_strategy_falls_back_to_bands_when_it_cannot_be_built(cfg, strategy_factory):
    from src.sim.coordinator import build_strategies
    from src.strategy.bands import BandsStrategy
    from src.strategy.peak import PeakStrategy

    def broken():
        raise ImportError("timesfm weights missing")

    strategy_factory["forecast"] = broken
    strategies, name, warning = build_strategies("forecast", cfg, LOCS, start_ts=START)
    assert name == "bands" and "ImportError: timesfm weights missing" in warning
    assert all(isinstance(s, BandsStrategy) for s in strategies.values())
    assert strategies["LZ_HOUSTON"] is not strategies["LZ_NORTH"]  # one instance per location
    peak, name, warning = build_strategies("peak", cfg, ["LZ_HOUSTON"], start_ts=START)
    assert name == "peak" and warning is None and isinstance(peak["LZ_HOUSTON"], PeakStrategy)


def test_forecast_strategy_needs_forecasts_covering_the_replay(cfg, strategy_factory):
    from src.sim.coordinator import build_strategies, forecast_coverage
    from src.strategy.forecast import ForecastStrategy

    strategy_factory["forecast"] = lambda: ForecastStrategy({}, duration_h=2.0)  # nothing cached
    _, name, warning = build_strategies("forecast", cfg, LOCS, start_ts=START)
    assert name == "bands" and "forecasts cover 0% of the first day" in warning

    covering = ForecastStrategy(_synthetic_books(START - 1800, 30), duration_h=2.0)
    assert forecast_coverage(covering, "LZ_NORTH", START, 300) == 1.0
    strategy_factory["forecast"] = lambda: covering
    strategies, name, warning = build_strategies("forecast", cfg, LOCS, start_ts=START)
    assert name == "forecast" and warning is None and strategies["LZ_HOUSTON"] is covering

    stale = ForecastStrategy(_synthetic_books(START - 40 * 3600, 30), duration_h=2.0)  # ended 10 h before start
    assert forecast_coverage(stale, "LZ_HOUSTON", START, 300) == 0.0
    from src.sim import coordinator

    coordinator._STRATEGY_CACHE.clear()
    strategy_factory["forecast"] = lambda: stale
    _, name, warning = build_strategies("forecast", cfg, LOCS, start_ts=START)
    assert name == "bands" and "cover 0%" in warning


def test_coordinator_drops_control_requests_the_api_gave_up_on():
    import time

    from src.sim.coordinator import Coordinator

    co = Coordinator(url="redis://127.0.0.1:1/0", prefix="test:stale:", fleet_size=10, workers=1)
    stale = co.handle_control({"id": None, "cmd": "reset", "wall": time.time() - 60})
    assert stale["error"] == "stale" and co.status == "idle"
