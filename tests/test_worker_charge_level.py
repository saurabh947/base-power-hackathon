"""Each controller reports the capacity-weighted average charge of the batteries it owns, and the share of usable energy
left above the 20% reserve (docs/API.md Worker: charge_level, usable_charge)."""
import numpy as np

from src.sim.coordinator import usable_kwh, worker_charge_level, worker_usable_charge


def test_capacity_weighted_average_of_owned_batteries():
    soc = np.array([0.2, 1.0, 0.5, 0.9])
    cap = np.array([10.0, 20.0, 10.0, 20.0])
    assert worker_charge_level(np.array([0, 1]), soc, cap) == round((0.2 * 10 + 1.0 * 20) / 30, 4)
    assert worker_charge_level(np.array([2]), soc, cap) == 0.5


def test_a_controller_with_no_batteries_has_no_charge_level():
    assert worker_charge_level(np.array([], dtype=np.int32), np.ones(3), np.ones(3)) is None


def test_usable_charge_is_the_share_above_the_reserve():
    soc = np.array([0.2, 1.0, 0.6, 0.9])
    cap = np.array([10.0, 20.0, 10.0, 20.0])
    level = (0.2 * 10 + 1.0 * 20) / 30
    assert worker_usable_charge(np.array([0, 1]), soc, cap) == round((level - 0.2) / 0.8, 4)
    assert worker_usable_charge(np.array([0]), soc, cap) == 0.0  # emptied by a crash: nothing usable
    assert worker_usable_charge(np.array([1]), soc, cap) == 1.0
    assert worker_usable_charge(np.array([2]), soc, cap) == 0.5  # halved: 0.2 + (1.0 - 0.2) / 2
    assert worker_usable_charge(np.array([], dtype=np.int32), soc, cap) is None
    # the same as the capacity-weighted usable energy over the usable capacity
    idx = np.array([0, 1, 2, 3])
    assert worker_usable_charge(idx, soc, cap) == round(usable_kwh(soc, cap) / (0.8 * cap.sum()), 4)
