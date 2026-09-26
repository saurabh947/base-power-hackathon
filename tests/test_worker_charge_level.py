"""Each controller reports the capacity-weighted average charge of the batteries it owns (docs/API.md Worker)."""
import numpy as np

from src.sim.coordinator import worker_charge_level


def test_capacity_weighted_average_of_owned_batteries():
    soc = np.array([0.2, 1.0, 0.5, 0.9])
    cap = np.array([10.0, 20.0, 10.0, 20.0])
    assert worker_charge_level(np.array([0, 1]), soc, cap) == round((0.2 * 10 + 1.0 * 20) / 30, 4)
    assert worker_charge_level(np.array([2]), soc, cap) == 0.5


def test_a_controller_with_no_batteries_has_no_charge_level():
    assert worker_charge_level(np.array([], dtype=np.int32), np.ones(3), np.ones(3)) is None
