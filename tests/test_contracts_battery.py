"""Contract tests for the BatteryAdapter seam (spec section 4, RUNLOG decisions).

Every BatteryAdapter implementation must pass this suite. To plug one in (e.g.
the Redis-hash adapter in M2), add a factory to FACTORIES. A factory takes the
initial BatteryStates and returns an adapter running 5-minute ticks with SoC
bounds [0.20, 1.00].

Physics under test (RUNLOG decision 1, all grid-side):
  discharge at P kW for dt h removes P*dt/sqrt(eta) kWh from storage,
  charge at |P| kW adds |P|*dt*sqrt(eta),
  |P| <= power_kw, and SoC stays within [0.20, 1.00].
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from src.core.ids import make_command_id
from src.core.interfaces import BatteryAdapter
from src.core.models import BatteryState, Command
from src.sim.adapters import InMemoryBatteryAdapter

UTC = timezone.utc
DT_H = 300 / 3600
SOC_MIN, SOC_MAX = 0.20, 1.00
T0 = datetime(2025, 5, 16, 5, 0, tzinfo=UTC)
FIVE = timedelta(minutes=5)
RUN_ID = "contract-run"


def _memory(states: list[BatteryState]) -> BatteryAdapter:
    return InMemoryBatteryAdapter(states, tick_seconds=300, soc_min=SOC_MIN, soc_max=SOC_MAX)


def _redis(states: list[BatteryState]) -> BatteryAdapter:
    """Per-battery Redis hashes (src/sim/redis_adapter.py); skips when Redis is unreachable."""
    import uuid

    from src.sim.redis_adapter import RedisBatteryAdapter
    from src.sim.redis_layout import reachable, redis_url

    if not reachable(redis_url()):
        pytest.skip(f"Redis not reachable at {redis_url()}")
    return RedisBatteryAdapter(states, url=redis_url(), prefix=f"test:contract:{uuid.uuid4().hex[:10]}:",
                               tick_seconds=300, soc_min=SOC_MIN, soc_max=SOC_MAX, ttl_s=600)


FACTORIES = [pytest.param(_memory, id="memory"), pytest.param(_redis, id="redis", marks=pytest.mark.redis)]


@pytest.fixture(params=FACTORIES)
def make_adapter(request):
    return request.param


def battery(bid="b0", *, capacity=13.5, power=5.0, soc=0.5, eff=0.90, available=True) -> BatteryState:
    return BatteryState(bid, capacity, power, soc, eff, available)


def cmd(bid: str, setpoint_kw: float, tick: int, command_id: str | None = None) -> Command:
    ts = T0 + tick * FIVE
    return Command(command_id or make_command_id(RUN_ID, bid, ts), bid, setpoint_kw, ts)


def soc_after_grid_kwh(soc: float, grid_kwh: float, capacity: float, eff: float) -> float:
    """Expected SoC after moving grid_kwh (+ sold, - bought) through the meter."""
    stored = -grid_kwh / math.sqrt(eff) if grid_kwh > 0 else -grid_kwh * math.sqrt(eff)
    return soc + stored / capacity


# --- read ------------------------------------------------------------------------


def test_read_returns_the_seeded_state(make_adapter):
    a = battery("a", capacity=13.5, power=5.0, soc=0.5)
    b = battery("b", capacity=18.2, power=11.5, soc=0.9, eff=0.85, available=False)
    adapter = make_adapter([a, b])
    assert adapter.read("a") == a
    assert adapter.read("b") == b


def test_read_unknown_id_raises_keyerror(make_adapter):
    adapter = make_adapter([battery("a")])
    with pytest.raises(KeyError):
        adapter.read("nope")


def test_apply_unknown_id_raises_keyerror(make_adapter):
    adapter = make_adapter([battery("a")])
    with pytest.raises(KeyError):
        adapter.apply(cmd("nope", 1.0, 0))


def test_read_returns_a_snapshot(make_adapter):
    adapter = make_adapter([battery("a", soc=0.5)])
    snap = adapter.read("a")
    snap.soc = 0.99
    snap.available = False
    assert adapter.read("a").soc == 0.5
    assert adapter.read("a").available is True


# --- physics -----------------------------------------------------------------------


def test_discharge_removes_p_dt_over_sqrt_eta_from_storage(make_adapter):
    adapter = make_adapter([battery("a", capacity=13.5, power=5.0, soc=0.5)])
    adapter.apply(cmd("a", 4.0, 0))
    expected = 0.5 - 4.0 * DT_H / math.sqrt(0.90) / 13.5
    assert adapter.read("a").soc == pytest.approx(expected, abs=1e-12)


def test_charge_adds_p_dt_times_sqrt_eta_to_storage(make_adapter):
    adapter = make_adapter([battery("a", capacity=13.5, power=5.0, soc=0.5)])
    adapter.apply(cmd("a", -4.0, 0))
    expected = 0.5 + 4.0 * DT_H * math.sqrt(0.90) / 13.5
    assert adapter.read("a").soc == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("eff", [0.90, 0.85, 1.0])
def test_round_trip_returns_eta_of_the_grid_energy(make_adapter, eff):
    # Buy 5 kW x 6 ticks = 2.5 kWh from the grid, then sell eta * 2.5 kWh
    # (eta * 5 kW x 6 ticks). Storage must land exactly where it started.
    adapter = make_adapter([battery("a", capacity=13.5, power=5.0, soc=0.5, eff=eff)])
    for k in range(6):
        adapter.apply(cmd("a", -5.0, k))
    grid_in = 5.0 * DT_H * 6
    assert adapter.read("a").soc == pytest.approx(soc_after_grid_kwh(0.5, -grid_in, 13.5, eff), abs=1e-9)
    for k in range(6, 12):
        adapter.apply(cmd("a", eff * 5.0, k))
    assert adapter.read("a").soc == pytest.approx(0.5, abs=1e-9)


def test_grid_power_is_capped_at_power_kw(make_adapter):
    adapter = make_adapter([battery("a", capacity=20.0, power=5.0, soc=0.6)])
    adapter.apply(cmd("a", 50.0, 0))  # asks for 10x the rating
    after_discharge = adapter.read("a").soc
    assert after_discharge == pytest.approx(0.6 - 5.0 * DT_H / math.sqrt(0.9) / 20.0, abs=1e-12)
    adapter.apply(cmd("a", -50.0, 1))
    assert adapter.read("a").soc == pytest.approx(after_discharge + 5.0 * DT_H * math.sqrt(0.9) / 20.0,
                                                  abs=1e-12)


def test_soc_stays_in_bounds_under_extreme_commands(make_adapter):
    adapter = make_adapter([battery("a", capacity=10.0, power=10.0, soc=0.5)])
    seen = []
    for k in range(60):
        setpoint = 1e6 if (k // 15) % 2 == 0 else -1e6  # 15 ticks flat out each way
        adapter.apply(cmd("a", setpoint, k))
        soc = adapter.read("a").soc
        assert SOC_MIN - 1e-12 <= soc <= SOC_MAX + 1e-12
        seen.append(soc)
    assert min(seen) == pytest.approx(SOC_MIN, abs=1e-12)  # it does reach the floor
    assert max(seen) == pytest.approx(SOC_MAX, abs=1e-12)  # and the ceiling


def test_discharge_near_the_floor_is_clipped_to_the_floor(make_adapter):
    adapter = make_adapter([battery("a", capacity=10.0, power=5.0, soc=0.21)])
    adapter.apply(cmd("a", 5.0, 0))  # wants 0.4167 kWh; only 0.1 kWh stored above the floor
    assert adapter.read("a").soc == pytest.approx(SOC_MIN, abs=1e-12)
    adapter.apply(cmd("a", 5.0, 1))
    assert adapter.read("a").soc == pytest.approx(SOC_MIN, abs=1e-12)


def test_charge_near_the_ceiling_is_clipped_to_the_ceiling(make_adapter):
    adapter = make_adapter([battery("a", capacity=10.0, power=5.0, soc=0.99)])
    adapter.apply(cmd("a", -5.0, 0))
    assert adapter.read("a").soc == pytest.approx(SOC_MAX, abs=1e-12)


def test_zero_setpoint_changes_nothing(make_adapter):
    adapter = make_adapter([battery("a", soc=0.5)])
    adapter.apply(cmd("a", 0.0, 0))
    assert adapter.read("a").soc == 0.5


def test_unavailable_battery_moves_no_energy(make_adapter):
    adapter = make_adapter([battery("a", soc=0.5, available=False)])
    adapter.apply(cmd("a", 5.0, 0))
    adapter.apply(cmd("a", -5.0, 1))
    state = adapter.read("a")
    assert state.soc == 0.5
    assert state.available is False


def test_commands_touch_only_their_own_battery(make_adapter):
    adapter = make_adapter([battery("a", soc=0.5), battery("b", soc=0.7)])
    adapter.apply(cmd("a", 5.0, 0))
    assert adapter.read("a").soc < 0.5
    assert adapter.read("b").soc == 0.7


# --- idempotency and fencing ---------------------------------------------------------


def test_same_command_twice_has_the_effect_of_once(make_adapter):
    once = make_adapter([battery("a", soc=0.5)])
    twice = make_adapter([battery("a", soc=0.5)])
    c = cmd("a", 3.0, 0)
    once.apply(c)
    twice.apply(c)
    twice.apply(c)
    assert twice.read("a").soc == once.read("a").soc


def test_reused_command_id_is_deduped_even_with_a_later_tick(make_adapter):
    # Workers MUST dedupe on command_id (spec section 4), not only on the tick fence.
    adapter = make_adapter([battery("a", soc=0.5)])
    first = cmd("a", 3.0, 0)
    adapter.apply(first)
    after = adapter.read("a").soc
    adapter.apply(Command(first.command_id, "a", 3.0, first.tick_ts + FIVE))
    assert adapter.read("a").soc == after


def test_second_command_for_the_same_battery_and_tick_is_a_noop(make_adapter):
    adapter = make_adapter([battery("a", soc=0.5)])
    adapter.apply(cmd("a", 3.0, 0))
    after_first = adapter.read("a").soc
    adapter.apply(cmd("a", -5.0, 0, command_id="another-id-same-tick"))
    assert adapter.read("a").soc == after_first


def test_stale_tick_is_rejected(make_adapter):
    adapter = make_adapter([battery("a", soc=0.5)])
    adapter.apply(cmd("a", 3.0, 5))
    after = adapter.read("a").soc
    adapter.apply(cmd("a", -5.0, 4))  # older tick, new id: zombie/late writer
    adapter.apply(cmd("a", 5.0, 0))
    assert adapter.read("a").soc == after


def test_later_ticks_still_apply_after_a_noop(make_adapter):
    reference = make_adapter([battery("a", soc=0.5)])
    reference.apply(cmd("a", 3.0, 0))
    reference.apply(cmd("a", 3.0, 1))
    adapter = make_adapter([battery("a", soc=0.5)])
    adapter.apply(cmd("a", 3.0, 0))
    adapter.apply(cmd("a", 3.0, 0))  # duplicate: no-op
    adapter.apply(cmd("a", 3.0, 1))
    assert adapter.read("a").soc == pytest.approx(reference.read("a").soc, abs=1e-15)


@pytest.mark.parametrize("bad_setpoint", [math.nan, math.inf, -math.inf])
def test_non_finite_setpoint_is_rejected_without_side_effects(make_adapter, bad_setpoint):
    adapter = make_adapter([battery("a", soc=0.5)])
    with pytest.raises(ValueError):
        adapter.apply(cmd("a", bad_setpoint, 0, command_id="bad"))
    assert adapter.read("a").soc == 0.5
    adapter.apply(cmd("a", 3.0, 0))  # the fence did not advance: this tick still applies
    assert adapter.read("a").soc < 0.5


def test_naive_tick_is_rejected_without_side_effects(make_adapter):
    adapter = make_adapter([battery("a", soc=0.5)])
    naive = Command("naive-id", "a", 3.0, T0.replace(tzinfo=None))
    with pytest.raises(ValueError):
        adapter.apply(naive)
    assert adapter.read("a").soc == 0.5
    adapter.apply(cmd("a", 3.0, 0))
    assert adapter.read("a").soc < 0.5
