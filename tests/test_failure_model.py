"""The human's failure model (docs/API.md "Failure model"), through the real coordinator and worker processes (Redis;
400 batteries, 4 controllers of 100, real snapshot prices):

- Crash: the controller's batteries are emptied to the reserve (usable charge 0) in the SoC mirror, the battery hashes
  and the race shadow, before the hand-over, so the adopters load the emptied state; the hand-over stays within one
  tick, and tracking holds against the feasible target, which drops because the batteries are empty.
- Nothing restarts on its own: a user restart brings the controller back, it takes its original batteries back from
  the adopters (with whatever charge they have by then) with an owner-epoch bump, and worker_recovered goes out.
- Controller offline: the usable charge of its batteries is halved at that moment (mirror, hashes, the worker's own
  state, the race); they don't move while offline and resume from the halved level. City and battery offline don't.
- The potential fleet never loses charge, so the shortfall includes the energy a crash destroyed.
"""
from __future__ import annotations

import json
import os
import signal
import time
import uuid

import numpy as np
import pytest

from src.config import load_raw_config
from src.sim import battery_status as bstatus
from src.sim.battery import headroom_kw
from src.sim.coordinator import Coordinator, Orchestration, usable_kwh, worker_usable_charge
from src.sim.redis_layout import F_EPOCH, F_OWNER, F_SOC, F_TICK, reachable, redis_url

pytestmark = pytest.mark.redis
EVENING = "2025-05-16T22:50:00Z"  # 5:50 pm CDT: the evening peak sells at full power from 6 pm (tick 2)
MORNING = "2025-06-28T14:55:00Z"  # 9:55 am CDT: the evening peak charges from 10 am (tick 1) to 3 pm
FLOOR = 0.2


@pytest.fixture
def coordinator():
    made = []

    def make(**kw):
        url = redis_url()
        if not reachable(url):
            pytest.skip(f"Redis not reachable at {url}")
        kw.setdefault("orchestration", Orchestration.from_config(load_raw_config(), max_speed_deadline_ms=500.0))
        co = Coordinator(url=url, prefix=f"test:fm:{uuid.uuid4().hex[:8]}:", fleet_size=400, workers=4, journal=True,
                         **kw)
        made.append(co)
        co.boot()
        return co

    yield make
    for co in made:
        co.shutdown()
        co.cleanup_keys()


def hash_soc(co, idx) -> np.ndarray:
    return np.array([float(x) for x in co.read_hash_state((F_SOC,))[F_SOC]])[idx]


def race_soc(co, idx, fleet="race") -> np.ndarray:
    """The shadow fleet's SoC of these (global) batteries: the race, or the potential."""
    race = getattr(co.run, fleet)
    out = np.empty(len(idx))
    for loc, sl in co.loc_slice.items():
        sel = (idx >= sl.start) & (idx < sl.stop)
        out[sel] = race.cities[loc].soc[idx[sel] - sl.start]
    return out


def events(co, kind):
    return [e for e in co.events if e["kind"] == kind]


def wait_for_exit(co, h, timeout=5.0):
    """Run the supervision until it has seen the killed process exit (it polls every 0.2 s in serve mode)."""
    deadline = time.monotonic() + timeout
    while h.exit_code is None and time.monotonic() < deadline:
        co._supervise(force=True)
        time.sleep(0.01)
    assert h.exit_code is not None


def dead_letter_reasons(co) -> set:
    return {json.loads(f[b"j"])["reason"] for _, f in co.r.xrange(co.keys.dead_letter)}


def test_a_crash_empties_its_batteries_before_they_are_handed_over(coordinator):
    co = coordinator()
    co.start({"start_utc": EVENING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=12, internal=True)
    for _ in range(5):
        co.step()  # the evening peak sells at full power from tick 2
    victim = co.workers[3]  # 52 San Antonio + 48 Austin batteries
    cohort = victim.owned.copy()
    assert len(cohort) == 100 and (co.soc[cohort] > 0.5).all()
    lost = usable_kwh(co.soc[cohort], co.cap[cohort])
    potential_before = race_soc(co, cohort, "potential")
    os.kill(victim.pid, signal.SIGKILL)
    wait_for_exit(co, victim)
    # emptied at once, before any hand-over: the mirror, the hashes, the controller's gauge and the race's copies
    assert victim.status == "healthy" and (co.owner[cohort] == 3).all()  # not declared dead yet
    assert (co.soc[cohort] == FLOOR).all() and (hash_soc(co, cohort) == FLOOR).all()
    shown = victim.public(co.layout, co.soc, co.cap, co.soc_min, co.soc_max)
    assert shown["usable_charge"] == 0.0 and shown["charge_level"] == FLOOR and shown["batteries"] == 100
    assert (race_soc(co, cohort) == FLOOR).all()  # the race mirrors the emptying: it compares decisions only
    assert np.array_equal(race_soc(co, cohort, "potential"), potential_before)  # the potential never loses charge
    # the kill tick: the empty batteries have no discharge headroom, so the feasible target is exactly what the others
    # can deliver; the victim misses the tick, and tracking holds
    others = np.setdiff1d(np.arange(co.N), cohort)
    d, _ = headroom_kw(co.soc[others], co.cap[others], co.power[others], co.eff[others], np.ones(len(others), bool),
                       co.dt_h, co.soc_min, co.soc_max)
    full, _ = headroom_kw(np.full(len(cohort), 0.85), co.cap[cohort], co.power[cohort], co.eff[cohort],
                          np.ones(len(cohort), bool), co.dt_h, co.soc_min, co.soc_max)
    kill_tick = co.step()
    assert co.tick_log[-1]["reported"] == 3 and victim.status == "suspect"
    feasible = sum(co.tick_log[-1]["feasible_exact"].values())  # the tick's feasible fleet target, unrounded
    assert feasible == pytest.approx(float(d.sum()), rel=1e-12)  # the cohort adds nothing
    assert float(full.sum()) > 0.05 * float(d.sum())  # ... where it would have added ~its share, charged
    assert kill_tick["error_pct"] <= 5.0
    handed = co.step()  # the second miss: declared dead at the end of this tick and handed over within it
    assert victim.status == "dead" and not (co.owner[cohort] == 3).any()
    adopted = events(co, "worker_adopted")[-1]
    assert adopted["message"].endswith("were handed to the other 3 controllers in "
                                       f"{adopted['detail']['ms']:.0f} ms, emptied by the crash.") or \
        adopted["message"].endswith("in under 1 ms, emptied by the crash.")
    assert adopted["detail"]["emptied"] is True and adopted["detail"]["emptied_kwh"] == round(lost, 3)
    assert handed["error_pct"] <= 5.0
    # the adopters loaded the emptied state and command the cohort from the next tick: empty, they wait (0 kW) while
    # the others keep selling; the fleet's plan moved to the batteries that still have charge
    after = co.step()
    assert co.tick_log[-1]["reported"] == co.tick_log[-1]["sent"] == 3
    assert (co.soc[cohort] == FLOOR).all() and (hash_soc(co, cohort) == FLOOR).all()
    ticks = {int(float(t)) for t in np.array(co.read_hash_state((F_TICK,))[F_TICK])[cohort]}
    assert ticks == {int(co.run.last_ts)}  # commanded (at 0 kW) by the adopters at the latest tick
    codes = co.status_codes
    assert (codes[cohort] == bstatus.WAITING).all() and (codes[others] == bstatus.SELLING).mean() > 0.9
    assert after["error_pct"] <= 5.0
    while co.run.status == "running" and co.step() is not None:
        pass
    assert all(r["error_pct"] <= 5.0 for r in co.tick_log[5:])
    # the controller panel: a dead controller owns nothing, so it has no gauge
    dead = victim.public(co.layout, co.soc, co.cap, co.soc_min, co.soc_max)
    assert (dead["batteries"], dead["charge_level"], dead["usable_charge"], dead["by_location"]) == (0, None, None, {})
    # every delivery applied exactly once, with the crash's emptying in between: the hashes are the journal's replay
    assert np.array_equal(co.read_hash_soc(), co.replay_journal(0.9))
    assert "expired_owner_dead" in dead_letter_reasons(co)


def test_nothing_restarts_on_its_own_and_a_user_restart_takes_the_batteries_back(coordinator):
    co = coordinator()
    co.start({"start_utc": MORNING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.5},
             max_ticks=80, internal=True)
    co.step()
    co.step()  # charging since 10 am
    victim = co.workers[2]  # 100 Dallas-Fort Worth batteries
    home = np.flatnonzero(co.home == 2)
    assert np.array_equal(victim.owned, home)
    os.kill(victim.pid, signal.SIGKILL)
    for _ in range(3):
        co.step()
    assert victim.status == "dead" and len(victim.owned) == 0
    adopters = sorted({int(w) for w in co.owner[home]})
    assert adopters == [0, 1, 3] and all(co.workers[w].adopted_from == [2] for w in adopters)
    old_epoch = victim.epoch
    for _ in range(6):  # the adopters run the emptied batteries: it's cheap, so they recharge them
        co.step()
    assert victim.status == "dead" and victim.recovery is None  # nothing restarts on its own
    assert (co.soc[home] > FLOOR).all()
    # a user restart: 409 for a controller that isn't down, 404 for one that doesn't exist
    reply = co.handle_control({"cmd": "restart", "args": {"worker_id": 0}})
    assert (reply["status"], reply["error"]) == (409, "not_crashed")
    assert reply["message"] == "Controller 1 hasn't crashed, so there's nothing to restart."
    assert co.handle_control({"cmd": "restart", "args": {"worker_id": 9}})["error"] == "unknown_worker"
    reply = co.handle_control({"cmd": "restart", "args": {"worker_id": 2}})
    assert reply == {"ok": True, "result": {"worker_id": 2, "status": "starting"}}
    again = co.handle_control({"cmd": "restart", "args": {"worker_id": 2}})
    assert again["error"] == "not_crashed" and again["message"] == "Controller 3 is already restarting."
    assert victim.epoch > old_epoch and victim.pid is not None and victim.status == "starting"
    charge_then = co.soc[home].copy()
    t0 = time.monotonic()
    assert co.serve_until(lambda: events(co, "worker_recovered"), timeout_s=30)
    took = time.monotonic() - t0
    recovered = events(co, "worker_recovered")[0]
    # it took its original batteries back from the adopters, as they were, with an owner-epoch bump
    assert victim.status == "healthy" and np.array_equal(victim.owned, home) and (co.owner[home] == 2).all()
    state = co.read_hash_state((F_OWNER, F_EPOCH))
    assert {int(x) for x in np.array(state[F_OWNER])[home]} == {2}
    assert {int(x) for x in np.array(state[F_EPOCH])[home]} == {victim.epoch}
    for w in adopters:
        assert not np.isin(co.workers[w].owned, home).any() and co.workers[w].adopted_from == []
    detail = recovered["detail"]
    assert (detail["worker_id"], detail["controller"], detail["batteries"]) == (2, 3, 100)
    assert detail["taken_from"] == adopters and detail["by_location"] == {"LZ_NORTH": 100}
    usable = worker_usable_charge(home, co.soc, co.cap, co.soc_min, co.soc_max)
    assert 0 < detail["usable_charge"] and recovered["message"] == (
        f"Controller 3 is back and has taken back its 100 Dallas–Fort Worth batteries, now "
        f"{detail['usable_charge']:.0%} charged.")
    assert (co.soc[home] >= charge_then - 1e-12).all()  # not reset: whatever charge they had by then (or more)
    # it runs them from the next tick; no adopter writes them again; every delivery applied exactly once
    k = co.run.k
    for _ in range(3):
        co.step()
    assert co.run.k == k + 3 and all(r["reported"] == r["sent"] == 4 for r in co.tick_log[-3:])
    ticks = {int(float(t)) for t in np.array(co.read_hash_state((F_TICK,))[F_TICK])[home]}
    assert ticks == {int(co.run.last_ts)}
    assert victim.public(co.layout, co.soc, co.cap, co.soc_min, co.soc_max)["usable_charge"] == \
        worker_usable_charge(home, co.soc, co.cap, co.soc_min, co.soc_max)
    assert not dead_letter_reasons(co) & {"not_owned", "epoch_mismatch", "not_loaded", "stale"}
    assert np.array_equal(co.read_hash_soc(), co.replay_journal(0.5))
    assert all(r["error_pct"] <= 5.0 for r in co.tick_log[5:])
    print(f"\nuser restart -> worker_recovered in {took * 1000:.0f} ms (take-back {detail['take_back_ms']:.1f} ms)"
          f"; usable then {usable:.1%}")


def test_a_restart_while_paused_and_with_no_run(coordinator):
    co = coordinator(orchestration=Orchestration(hb_interval_s=0.2, hb_ttl_s=0.8))
    # no run: the crash is noticed at heartbeat expiry; a restart gives the controller its batteries back at once
    victim = co.workers[1]
    home = np.flatnonzero(co.home == 1)
    os.kill(victim.pid, signal.SIGKILL)
    assert co.serve_until(lambda: victim.status == "dead", timeout_s=5)
    assert not (co.owner[home] == 1).any()
    co.handle_control({"cmd": "restart", "args": {"worker_id": 1}})
    assert co.serve_until(lambda: events(co, "worker_recovered"), timeout_s=30)
    assert (co.owner[home] == 1).all() and victim.status == "healthy"
    assert events(co, "worker_recovered")[0]["message"] == (
        "Controller 2 is back and has taken back its 100 Houston and Dallas–Fort Worth batteries.")  # no run: no charge
    # paused: the crash and the take-back both happen while nothing ticks; the next tick runs them again
    co.start({"start_utc": EVENING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=30, internal=True)
    for _ in range(4):
        co.step()
    co.handle_control({"cmd": "pause"})
    victim = co.workers[2]
    home = np.flatnonzero(co.home == 2)
    os.kill(victim.pid, signal.SIGKILL)
    assert co.serve_until(lambda: victim.status == "dead", timeout_s=5)
    assert (co.soc[home] == FLOOR).all()
    co.handle_control({"cmd": "restart", "args": {"worker_id": 2}})
    assert co.serve_until(lambda: len(events(co, "worker_recovered")) == 2, timeout_s=30)
    assert co.run.status == "paused" and co.run.k == 4 and (co.owner[home] == 2).all()
    assert events(co, "worker_recovered")[1]["message"].endswith("batteries, starting from empty.")
    co.handle_control({"cmd": "resume"})
    for _ in range(3):
        co.step()
    assert all(r["reported"] == r["sent"] == 4 for r in co.tick_log[-3:])
    assert np.array_equal(co.read_hash_soc(), co.replay_journal(0.9))


def test_taking_a_controller_offline_halves_its_usable_charge(coordinator):
    co = coordinator()
    co.start({"start_utc": EVENING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=20, internal=True)
    for _ in range(4):
        co.step()
    mine = np.flatnonzero(co.owner == 1)
    before, race_before = co.soc[mine].copy(), race_soc(co, mine)
    potential_before = race_soc(co, mine, "potential")
    usable_before = worker_usable_charge(mine, co.soc, co.cap, co.soc_min, co.soc_max)
    reply = co.handle_control({"cmd": "availability", "args": {"scope": "worker", "worker_id": 1, "available": False}})
    assert reply["ok"] and reply["result"]["affected"] == 100
    halved = FLOOR + (before - FLOOR) / 2
    assert np.array_equal(co.soc[mine], halved) and np.array_equal(hash_soc(co, mine), halved)
    assert np.array_equal(race_soc(co, mine), FLOOR + (race_before - FLOOR) / 2)  # the race mirrors it
    assert np.array_equal(race_soc(co, mine, "potential"), potential_before)  # the potential doesn't
    assert co.workers[1].public(co.layout, co.soc, co.cap, co.soc_min, co.soc_max)["usable_charge"] == \
        pytest.approx(usable_before / 2, abs=1e-4)
    message = events(co, "availability_changed")[-1]["message"]
    assert message.startswith("Controller 2's 100 batteries in ") and \
        " are offline and have lost half of their usable charge; 300 of 400 batteries are online." in message
    # asking again for a controller that is already offline changes nothing
    co.handle_control({"cmd": "availability", "args": {"scope": "worker", "worker_id": 1, "available": False}})
    assert np.array_equal(co.soc[mine], halved)
    # while offline they don't move, and the others keep tracking the (smaller) feasible target
    for _ in range(3):
        rec = co.step()
        assert rec["error_pct"] <= 5.0
    assert np.array_equal(co.soc[mine], halved) and np.array_equal(hash_soc(co, mine), halved)
    # only a user brings them back; back online they sell from the halved level, as the worker itself holds it: the
    # hashes (written by the worker from its own state) equal the journal's replay with the halving in between
    co.handle_control({"cmd": "availability", "args": {"scope": "worker", "worker_id": 1, "available": True}})
    assert np.array_equal(co.soc[mine], halved)
    rec = co.step()
    assert (co.soc[mine] < halved).all() and rec["error_pct"] <= 5.0
    assert np.array_equal(co.read_hash_soc(), co.replay_journal(0.9))
    # city-level and single-battery offline don't change charge
    snapshot = co.soc.copy()
    co.handle_control({"cmd": "availability", "args": {"scope": "location", "location": "LZ_NORTH",
                                                       "offline_fraction": 0.5}})
    co.handle_control({"cmd": "availability", "args": {"scope": "battery", "battery_id": "b00005", "available": False}})
    co.handle_control({"cmd": "availability", "args": {"scope": "all", "available": False}})
    assert np.array_equal(co.soc, snapshot)
    assert np.array_equal(co.read_hash_soc(), co.replay_journal(0.9))


def test_a_halving_waits_for_the_controllers_late_tick(coordinator):
    """A controller taken offline while one of its ticks is still in flight (it missed the deadline): the halving
    waits until that tick's late report is booked, then halves the SoC that tick left, so nothing is metered from a
    halved SoC and the worker never applies a tick over a changed one."""
    co = coordinator()
    co.start({"start_utc": EVENING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=20, internal=True)
    for _ in range(3):
        co.step()
    h = co.workers[1]
    mine = np.flatnonzero(co.owner == 1)
    os.kill(h.pid, signal.SIGSTOP)
    try:
        co.step()  # it misses tick 3: suspect, with tick 3 in flight
        assert h.status == "suspect" and 3 in h.inflight
        before = co.soc[mine].copy()
        co.handle_control({"cmd": "availability", "args": {"scope": "worker", "worker_id": 1, "available": False}})
        assert len(h.pending_halve) == 100 and np.array_equal(co.soc[mine], before)  # not yet
        co._between_ticks()
        assert len(h.pending_halve) == 100  # still frozen: still waiting
    finally:
        os.kill(h.pid, signal.SIGCONT)
    deadline = time.monotonic() + 10
    while len(h.pending_halve) and time.monotonic() < deadline:
        time.sleep(0.02)
        co._between_ticks()  # its late tick-3 report is booked first, then the halving
    assert not len(h.pending_halve) and not h.inflight
    assert (co.soc_k[mine] >= 3).all() and (co.soc[mine] < before).all()  # halved from after its tick 3
    halved = co.soc[mine].copy()
    assert np.array_equal(hash_soc(co, mine), halved)
    for _ in range(3):
        co.step()
    assert np.array_equal(co.soc[mine], halved)  # offline: frozen at the halved level
    assert np.array_equal(co.read_hash_soc(), co.replay_journal(0.9))


def test_the_potential_shortfall_includes_the_energy_a_crash_destroyed(coordinator):
    """Same evening twice: without and with a crash after tick 5. The potential never loses charge, so it earns the
    no-crash revenue bit for bit, and the shortfall is exactly what the crashed cohort would have earned from the kill
    on (the evening peak sells every battery at its own feasible maximum, so the others are unaffected)."""
    co = coordinator()
    settings = {"start_utc": EVENING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9}
    co.start(settings, max_ticks=14, internal=True)
    while co.step() is not None:
        pass
    clean = co.run.revenue_usd
    assert co.run.potential.running_usd == clean
    cohort = np.flatnonzero(co.home == 3)
    lost_revenue = 0.0  # what the cohort earned from tick 6 on, from every report's delivered kW
    for k in range(6, 14):
        ts = co.run.start_ts + k * co.tick_s
        for idx, kw in co.journal[k]:
            sel = np.isin(idx, cohort)
            for loc in co.layout.locations:
                here = sel & (co.layout.loc_of[idx] == co.layout.locations.index(loc))
                price = co.run.prices.at(loc, ts).price_usd_mwh
                lost_revenue += float(kw[here].sum()) * co.dt_h * price / 1000.0
    assert lost_revenue > 0
    co.start(settings, max_ticks=14, internal=True)
    fired = {}

    def kill(rec):
        if rec["k"] == 5 and not fired:
            fired["pid"] = co.workers[3].pid
            os.kill(fired["pid"], signal.SIGKILL)

    co.tick_hooks.append(kill)
    while co.step() is not None:
        pass
    assert fired and co.workers[3].status == "dead"
    potential = co.run.potential.running_usd
    assert potential == clean  # the potential: no outage, no lost charge
    shortfall = co.snapshot()["race"]["potential"]["shortfall_usd"]
    assert shortfall == pytest.approx(potential - co.run.revenue_usd, abs=1e-6)
    assert shortfall == pytest.approx(lost_revenue, rel=1e-6)  # the energy the crash destroyed, at its prices
    print(f"\nshortfall ${shortfall:,.2f} = the crashed cohort's lost sales ${lost_revenue:,.2f}")
