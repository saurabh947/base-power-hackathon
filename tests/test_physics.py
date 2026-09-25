"""Unit tests for the vectorized physics (src/sim/battery.py) and allocation (src/sim/allocation.py)."""

import math

import numpy as np
import pytest

from src.sim.allocation import allocate, fleet_headroom_kw
from src.sim.battery import fleet_soc, grid_kwh_from_soc_change, headroom_kw, step

DT = 300 / 3600
R = math.sqrt(0.9)


def random_fleet(n=20000, seed=3):
    rng = np.random.default_rng(seed)
    return dict(
        soc=rng.uniform(0.2, 1.0, n),
        capacity_kwh=rng.uniform(10.0, 20.0, n),
        power_kw=rng.uniform(5.0, 11.5, n),
        efficiency=np.full(n, 0.9),
        available=rng.random(n) > 0.1,
    )


# --- headroom ------------------------------------------------------------------------


def test_headroom_formulas_per_battery():
    soc = np.array([0.5, 0.2, 1.0, 0.21, 0.99])
    capacity = np.full(5, 10.0)
    power = np.full(5, 5.0)
    discharge, charge = headroom_kw(soc, capacity, power, 0.9, True, DT)
    assert discharge == pytest.approx([5.0, 0.0, 5.0, 0.01 * 10 * R / DT, 5.0], abs=1e-12)
    assert charge == pytest.approx([5.0, 5.0, 0.0, 5.0, 0.01 * 10 / (R * DT)], abs=1e-12)


def test_headroom_is_zero_for_unavailable_and_never_negative():
    discharge, charge = headroom_kw([0.5, 0.1, 1.2], [10.0] * 3, [5.0] * 3, 0.9, [False, True, True], DT)
    assert discharge.tolist() == [0.0, 0.0, 5.0]  # 0.1 is below the floor: no discharge
    assert charge.tolist() == [0.0, 5.0, 0.0]  # 1.2 is above the ceiling: no charge


# --- step ---------------------------------------------------------------------------


def test_step_moves_storage_by_the_grid_side_formulas():
    soc = np.array([0.5, 0.5, 0.5])
    new_soc, delivered = step(soc, 10.0, 5.0, 0.9, True, [4.0, -4.0, 0.0], DT)
    assert delivered.tolist() == [4.0, -4.0, 0.0]
    assert new_soc == pytest.approx([0.5 - 4 * DT / R / 10, 0.5 + 4 * DT * R / 10, 0.5], abs=1e-15)
    assert soc.tolist() == [0.5, 0.5, 0.5]  # inputs are not mutated


def test_step_clips_to_power_and_soc_bounds_and_skips_unavailable():
    soc = np.array([0.5, 0.21, 0.99, 0.5])
    new_soc, delivered = step(soc, 10.0, 5.0, 0.9, [True, True, True, False], [50.0, 5.0, -5.0, 5.0], DT)
    assert delivered == pytest.approx([5.0, 0.01 * 10 * R / DT, -0.01 * 10 / (R * DT), 0.0], abs=1e-12)
    assert 0.2 <= new_soc[1] <= 0.2 + 1e-15  # driven onto the floor, never through it
    assert 1.0 - 1e-15 <= new_soc[2] <= 1.0  # and onto the ceiling
    assert new_soc[3] == 0.5


def test_step_never_teleports_an_out_of_range_soc():
    new_soc, delivered = step([0.1, 0.1, 1.2], 10.0, 5.0, 0.9, True, [0.0, 5.0, -5.0], DT)
    assert delivered.tolist() == [0.0, 0.0, 0.0]
    assert new_soc.tolist() == [0.1, 0.1, 1.2]


def test_step_rejects_non_finite_setpoints():
    with pytest.raises(ValueError):
        step([0.5, 0.5], 10.0, 5.0, 0.9, True, [1.0, math.nan], DT)


def test_step_on_a_20k_fleet_respects_every_limit_and_meters_exactly():
    f = random_fleet()
    rng = np.random.default_rng(9)
    setpoint = rng.uniform(-30.0, 30.0, f["soc"].size)
    new_soc, delivered = step(**f, setpoint_kw=setpoint, dt_h=DT)
    assert np.all((new_soc >= 0.2) & (new_soc <= 1.0))
    assert np.all(np.abs(delivered) <= f["power_kw"])
    assert np.all(delivered[~f["available"]] == 0.0)
    metered = grid_kwh_from_soc_change(f["soc"], new_soc, f["capacity_kwh"], f["efficiency"])
    assert metered == pytest.approx(delivered * DT, abs=1e-12)  # read()-based metering inverts step()


def test_vectorized_round_trip_returns_eta():
    soc0 = np.array([0.3, 0.5, 0.7])
    capacity, power = np.array([10.0, 13.5, 20.0]), np.array([5.0, 7.5, 11.5])
    soc, bought = soc0, 0.0
    for _ in range(4):
        soc, d = step(soc, capacity, power, 0.9, True, -power, DT)
        bought -= d * DT
    sold = 0.0
    for _ in range(4):
        soc, d = step(soc, capacity, power, 0.9, True, 0.9 * power, DT)
        sold += d * DT
    assert soc == pytest.approx(soc0, abs=1e-12)
    assert sold / bought == pytest.approx(np.full(3, 0.9), abs=1e-12)


def test_fleet_soc_is_capacity_weighted_over_available_batteries():
    assert fleet_soc([0.2, 1.0, 0.9], [10.0, 30.0, 50.0], [True, True, False]) == pytest.approx(0.8)
    assert fleet_soc([0.5], [10.0], [False]) == 0.0


# --- allocation ----------------------------------------------------------------------


def test_fleet_headroom_sums_power_of_available_batteries():
    assert fleet_headroom_kw([5.0, 7.5, 11.5], [True, False, True]) == 16.5


def test_allocate_splits_in_proportion_to_directional_headroom():
    feasible, sp = allocate(3.0, [1.0, 2.0, 3.0], [9.0, 9.0, 9.0])
    assert feasible == 3.0
    assert sp == pytest.approx([0.5, 1.0, 1.5])
    feasible, sp = allocate(-6.0, [9.0, 9.0, 9.0], [1.0, 2.0, 3.0])
    assert feasible == -6.0
    assert sp == pytest.approx([-1.0, -2.0, -3.0])


def test_allocate_clips_to_feasible_headroom_and_skips_zero_headroom():
    feasible, sp = allocate(100.0, [1.0, 0.0, 3.0], [5.0, 5.0, 5.0])
    assert feasible == 4.0
    assert sp.tolist() == [1.0, 0.0, 3.0]
    assert allocate(-5.0, [1.0, 1.0], [0.0, 0.0])[0] == 0.0
    feasible, sp = allocate(0.0, [1.0, 1.0], [1.0, 1.0])
    assert feasible == 0.0 and sp.tolist() == [0.0, 0.0]


def test_allocate_never_exceeds_any_battery_headroom():
    f = random_fleet(n=5000, seed=11)
    discharge, charge = headroom_kw(**f, dt_h=DT)
    for target in (1e9, discharge.sum(), 0.5 * discharge.sum(), -1e9, -charge.sum(), -0.3 * charge.sum()):
        feasible, sp = allocate(float(target), discharge, charge)
        assert np.all(sp <= discharge) and np.all(-sp <= charge)
        assert sp.sum() == pytest.approx(feasible, rel=1e-12)
        new_soc, delivered = step(**f, setpoint_kw=sp, dt_h=DT)
        assert delivered == pytest.approx(sp, abs=1e-12)  # a feasible allocation is delivered as sent


def test_allocate_rejects_non_finite_targets():
    with pytest.raises(ValueError):
        allocate(math.inf, [1.0], [1.0])
