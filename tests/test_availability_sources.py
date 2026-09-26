"""Which controller's offline switch took each battery down (the human's rule, 2026-09-26): bringing that controller
back online returns all of them wherever they are now, and a restarted controller gets back online the batteries
that went down only because another controller ran them at the time. User-chosen offline stays offline."""
import numpy as np

from src.config import load_config
from src.sim import control as ctl
from src.sim.redis_layout import FleetLayout


def setup():
    layout = FleetLayout.build(load_config(), size=400, workers=4)
    owners = layout.default_owners(4).copy()
    return layout, owners, ctl.AvailabilityState(layout, seed=42)


def test_a_restart_brings_back_batteries_another_controllers_switch_took_down():
    layout, owners, av = setup()
    home2 = np.flatnonzero(owners == 2)
    owners[home2[:40]] = 0  # controller 3 crashed: controller 1 runs 40 of its batteries
    av.apply({"scope": "worker", "worker_id": 0, "available": False}, owners, 4)  # controller 1 switched off
    assert av.offline[home2[:40]].all() and (av.offline_by[home2[:40]] == 0).all()
    owners[home2] = 2  # the restart takes them back
    rel = av.release_collateral(home2, 2, owners)
    assert len(rel["changed_idx"]) == 40 and rel["from"] == {0: 40}
    assert not av.offline[home2].any()
    assert av.offline[owners == 0].all()  # controller 1's own batteries stay offline: its switch is still off


def test_own_switch_and_direct_user_choices_stay_offline_on_restart():
    layout, owners, av = setup()
    home1 = np.flatnonzero(owners == 1)
    av.apply({"scope": "worker", "worker_id": 1, "available": False}, owners, 4)  # its own switch
    bid = layout.ids[int(home1[5])]
    av.apply({"scope": "battery", "battery_id": bid, "available": False}, owners, 4)  # a direct choice
    rel = av.release_collateral(home1, 1, owners)
    assert len(rel["changed_idx"]) == 0 and av.offline[home1].all()


def test_bringing_a_controller_back_online_returns_everything_its_switch_took_down():
    layout, owners, av = setup()
    home2 = np.flatnonzero(owners == 2)
    owners[home2[:40]] = 0
    av.apply({"scope": "worker", "worker_id": 0, "available": False}, owners, 4)
    owners[home2] = 2  # those 40 went back to controller 3 while still offline
    av.apply({"scope": "worker", "worker_id": 0, "available": True}, owners, 4)
    assert not av.offline.any() and (av.offline_by == -1).all()


def test_a_city_slider_resets_the_source_to_a_direct_choice():
    layout, owners, av = setup()
    av.apply({"scope": "worker", "worker_id": 0, "available": False}, owners, 4)
    loc = layout.locations[0]
    av.apply({"scope": "location", "location": loc, "offline_fraction": 1.0}, owners, 4)
    idx = layout.location_indices(0)
    assert av.offline[idx].all() and (av.offline_by[idx] == -1).all()
    rel = av.release_collateral(idx, 3, owners)  # nothing here is anyone's switch any more
    assert len(rel["changed_idx"]) == 0
