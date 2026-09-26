"""M3 acceptance: kill any worker mid-run and the fleet recovers.

- The dead worker's cohort is reassigned within 1 tick of the DEAD declaration.
- Fleet output is back within 5% of the feasible target within 3 ticks of the kill.
- Failures are visible in dead_letter; nothing is lost or applied twice (a replay of every
  reported delivery through src.sim.battery, with the crash's emptying in between, equals the battery hashes
  exactly).
- The live map sees it: battery_status shows the cohort as "controller down" (4) from the kill until
  the hand-over, and workers[].by_location moves its batteries to the adopters.
- The human's crash rule (docs/API.md failure model): the crash empties the cohort to the 20% reserve before the
  hand-over, so during max discharge the adopters have nothing to sell from it: it waits (0 kW) from then on.
Both at max speed and at the demo's 2 ticks/s, during max discharge (RUNLOG decision 8). Worker 17 is the
controller whose batteries sit in two cities (600 San Antonio + 400 Austin).
"""
import os
import signal
import time
import uuid

import numpy as np
import pytest

from src.config import load_raw_config
from src.sim import battery_status as bstatus
from src.sim import geo
from src.sim.coordinator import Coordinator, Orchestration
from src.sim.redis_layout import F_OWNER, F_TICK, reachable, redis_url

pytestmark = pytest.mark.redis
EVENING = "2025-05-16T22:50:00Z"  # 5:50 pm CDT: the peak rule starts selling at tick 2 (6 pm)


@pytest.fixture
def coordinator():
    made = []

    def make(**kw):
        url = redis_url()
        if not reachable(url):
            pytest.skip(f"Redis not reachable at {url}")
        co = Coordinator(url=url, prefix=f"test:m3:{uuid.uuid4().hex[:8]}:", journal=True, **kw)
        made.append(co)
        co.boot()
        return co

    yield make
    for co in made:
        co.shutdown()
        co.cleanup_keys()


def _cont(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGCONT)
    except ProcessLookupError:  # already gone (and reaped)
        pass


def _event(co, kind, worker_id):
    return next((e for e in co.events if e["kind"] == kind and e["detail"].get("worker_id") == worker_id), None)


def _kill_after(co, tick: int, worker_id: int, sig=signal.SIGKILL) -> dict:
    fired = {"codes": {}, "down_elsewhere": {}}

    def hook(rec):
        if "cohort" in fired:  # battery_status after each tick: the victim's batteries, and everyone else's 4s
            fired["codes"][rec["k"]] = co.status_codes[fired["cohort"]].copy()
            others = np.ones(co.N, dtype=bool)
            others[fired["cohort"]] = False
            fired["down_elsewhere"][rec["k"]] = int((co.status_codes[others] == bstatus.CONTROLLER_DOWN).sum())
        if rec["k"] == tick and "pid" not in fired:
            victim = co.workers[worker_id]
            fired.update(pid=victim.pid, cohort=victim.owned.copy(),
                         by_location_before={h.worker_id: h.public(co.layout)["by_location"] for h in co.workers})
            os.kill(victim.pid, sig)

    co.tick_hooks.append(hook)
    return fired


def _assert_recovered(co, killed_after: int, victim: int, cohort: np.ndarray, soc0: float,
                      fired: dict | None = None) -> None:
    log = {r["k"]: r for r in co.tick_log}
    dead = _event(co, "worker_dead", victim)
    adopted = _event(co, "worker_adopted", victim)
    assert dead is not None and adopted is not None, [e["kind"] for e in co.events]
    declared = dead["detail"]["tick_index"]
    assert dead["detail"]["reason"] == "missed_deadlines"
    assert declared == killed_after + 2  # missed ticks killed_after+1 and +2
    assert adopted["detail"]["reassigned_for_tick_index"] - declared <= 1
    assert adopted["detail"]["batteries"] == len(cohort) == 1000
    assert len(adopted["detail"]["adopted_by"]) == 19 and victim not in adopted["detail"]["adopted_by"]
    # docs/API.md: detail.worker_id is 0-based, detail.controller and every message say Controller N+1
    for e in (dead, adopted, _event(co, "worker_suspect", victim)):
        assert e["detail"]["worker_id"] == victim and e["detail"]["controller"] == victim + 1
        assert e["message"].startswith(f"Controller {victim + 1}")
    assert adopted["detail"]["ms"] < 50
    cities = co.layout.counts_by_location(cohort)  # its message names the cities it served
    assert adopted["detail"]["by_location"] == cities
    assert f" batteries in {geo.join_names([geo.city_name(loc) for loc in cities])} were handed" in adopted["message"]
    if fired is not None:
        # battery_status: "controller down" (4) for exactly its batteries from the kill until the hand-over ...
        for k in range(killed_after + 1, declared + 1):
            assert (fired["codes"][k] == bstatus.CONTROLLER_DOWN).all(), k
            assert fired["down_elsewhere"][k] == 0, k
        # ... then back to what they do: the crash emptied them to the 20% reserve, so at max discharge the
        # adopters have nothing to sell from them: they wait (0 kW)
        after = fired["codes"][declared + 1]
        assert not (after == bstatus.CONTROLLER_DOWN).any() and (after == bstatus.WAITING).all()
        assert (co.soc[cohort] == 0.2).all()
        assert all(n == 0 for k, n in fired["down_elsewhere"].items() if k > declared)
        # workers[].by_location: the cohort's cities moved to the adopters; the dead worker has none
        before = fired["by_location_before"]
        now = {h.worker_id: h.public(co.layout)["by_location"] for h in co.workers}
        assert now[victim] == {} and before[victim] == cities
        for loc, n in cities.items():
            gained = sum(now[w].get(loc, 0) - before[w].get(loc, 0) for w in now if w != victim)
            assert gained == n, loc
    # the very next tick after the declaration commands the whole cohort through its adopters
    nxt = log[declared + 1]
    assert nxt["reported"] == nxt["sent"] == 19
    owners = {int(o) for o in np.array(co.read_hash_state((F_OWNER,))[F_OWNER])[cohort]}
    assert victim not in owners and len(owners) == 19
    # within 3 ticks of the kill (and from then on) actual tracks the feasible target within 5%
    for k in range(killed_after + 3, max(log) + 1):
        assert log[k]["error_pct"] <= 5.0, (k, log[k]["error_pct"])
    assert all(log[k]["target_kw"]["fleet"] > 0 for k in range(killed_after + 1, killed_after + 4))  # max discharge
    print(f"\nkill after tick {killed_after}: error% by tick "
          f"{[(k, round(log[k]['error_pct'], 3)) for k in range(killed_after, killed_after + 5)]}; "
          f"handover {adopted['detail']['ms']:.1f} ms")
    # failures are visible in dead_letter
    count = int(co.r.get(co.keys.dead_letter_count))
    reasons = {e[1][b"j"].split(b'"reason": "')[1].split(b'"')[0].decode() for e in co.r.xrange(co.keys.dead_letter)}
    assert count >= 1000 and "expired_owner_dead" in reasons
    # every battery kept being commanded after the handover, and nothing was double-applied
    last_ts = int(co.run.last_ts)
    assert {int(float(t)) for t in co.read_hash_state((F_TICK,))[F_TICK]} == {last_ts}
    assert np.array_equal(co.read_hash_soc(), co.replay_journal(soc0))


def _wide_max_speed_barrier() -> Orchestration:
    """500 ms at max speed so a busy laptop never makes a *healthy* worker miss (production: 250 ms)."""
    return Orchestration.from_config(load_raw_config(), max_speed_deadline_ms=500.0)


def test_kill_a_worker_at_max_speed(coordinator):
    co = coordinator(orchestration=_wide_max_speed_barrier())
    co.start({"start_utc": EVENING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=16, internal=True)
    fired = _kill_after(co, 5, 17)  # the controller of 600 San Antonio + 400 Austin batteries
    assert co.serve_until(lambda: co.run.status == "finished", timeout_s=120)
    assert "pid" in fired
    assert co.layout.counts_by_location(fired["cohort"]) == {"LZ_CPS": 600, "LZ_AEN": 400}
    _assert_recovered(co, 5, 17, fired["cohort"], 0.9, fired)
    assert co.workers[17].status == "dead" and co.workers[17].exit_code == -signal.SIGKILL


def test_kill_a_worker_at_two_ticks_per_second(coordinator):
    co = coordinator()
    co.start({"start_utc": EVENING, "speed_tps": 2, "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=12, internal=True)
    fired = _kill_after(co, 4, 13)  # a Dallas-Fort Worth controller
    t0 = time.monotonic()
    assert co.serve_until(lambda: co.run.status == "finished", timeout_s=60)
    assert time.monotonic() - t0 >= 11 * 0.5 - 0.1  # it really ran at 2 ticks/s
    assert co.layout.counts_by_location(fired["cohort"]) == {"LZ_NORTH": 1000}
    _assert_recovered(co, 4, 13, fired["cohort"], 0.9, fired)


def test_a_frozen_worker_is_fenced_out_when_it_wakes(coordinator):
    co = coordinator(orchestration=_wide_max_speed_barrier())
    co.start({"start_utc": EVENING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=14, internal=True)
    fired = _kill_after(co, 3, 4, sig=signal.SIGSTOP)
    woke = {}

    def wake(rec):
        if rec["k"] == 9 and not woke:
            _cont(fired["pid"])
            woke["at"] = rec["k"]

    co.tick_hooks.append(wake)
    try:
        assert co.serve_until(lambda: co.run.status == "finished", timeout_s=120)
    finally:
        _cont(fired["pid"])
    zombie = co.workers[4].proc
    assert zombie.wait(timeout=10) == 3  # it found its lease revoked and left without writing
    reasons = [e[1][b"j"] for e in co.r.xrange(co.keys.dead_letter)]
    assert any(b"epoch_mismatch" in r for r in reasons)
    _assert_recovered(co, 3, 4, fired["cohort"], 0.9)


def test_one_missed_deadline_is_suspect_not_dead(coordinator):
    co = coordinator(orchestration=_wide_max_speed_barrier())
    co.start({"start_utc": EVENING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=10, internal=True)
    fired = _kill_after(co, 3, 2, sig=signal.SIGSTOP)
    resumed = {}

    def resume(rec):
        if rec["k"] == 4 and not resumed:
            _cont(fired["pid"])
            resumed["at"] = 4

    co.tick_hooks.append(resume)
    try:
        assert co.serve_until(lambda: co.run.status == "finished", timeout_s=60)
    finally:
        _cont(fired["pid"])
    log = {r["k"]: r for r in co.tick_log}
    assert _event(co, "worker_suspect", 2) is not None and _event(co, "worker_dead", 2) is None
    assert co.workers[2].status == "healthy" and log[4]["healthy_workers"] == 19 and log[6]["healthy_workers"] == 20
    assert log[5]["error_pct"] <= 5.0 and log[6]["error_pct"] <= 5.0  # suspect batteries got 0; survivors re-split
    assert int(co.r.get(co.keys.dead_letter_count) or 0) == 0
    # its late tick-4 telemetry was still booked: hashes == replay of every reported delivery
    assert np.array_equal(co.read_hash_soc(), co.replay_journal(0.9))


def test_heartbeat_expiry_detects_a_death_while_paused(coordinator):
    orch = Orchestration(hb_interval_s=0.2, hb_ttl_s=0.8)
    co = coordinator(fleet_size=400, workers=4, orchestration=orch)
    co.start({"start_utc": EVENING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=12, internal=True)
    assert co.serve_until(lambda: co.run.k >= 4, timeout_s=30)
    co.handle_control({"cmd": "pause"})
    victim = co.workers[1]
    cohort = victim.owned.copy()
    os.kill(victim.pid, signal.SIGKILL)
    t0 = time.monotonic()
    assert co.serve_until(lambda: victim.status == "dead", timeout_s=5)
    assert time.monotonic() - t0 < 2.0
    dead = _event(co, "worker_dead", 1)
    assert dead["detail"]["reason"] == "heartbeat_expired"
    assert sorted(np.concatenate([h.owned for h in co.workers if h.worker_id != 1]).tolist()) == list(range(400))
    co.handle_control({"cmd": "resume"})
    assert co.serve_until(lambda: co.run.status == "finished", timeout_s=30)
    tail = [r for r in co.tick_log if r["k"] >= 4]
    assert tail and all(r["error_pct"] <= 5.0 for r in tail)
    assert np.array_equal(co.read_hash_soc(), co.replay_journal(0.9))
    assert len(cohort) == 100


def test_reset_restarts_killed_workers_and_clears_availability(coordinator):
    co = coordinator(fleet_size=400, workers=4)
    co.start({"start_utc": EVENING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=40, internal=True)
    co.handle_control({"cmd": "availability", "args": {"scope": "location", "location": "LZ_NORTH",
                                                       "offline_fraction": 0.5}})
    fired = _kill_after(co, 2, 3)
    assert co.serve_until(lambda: co.workers[3].status == "dead", timeout_s=30)
    old_pids = {h.pid for h in co.workers}
    reply = co.handle_control({"cmd": "reset"})
    assert reply == {"ok": True, "result": {"status": "idle"}}
    assert co.status == "idle" and co.run is None
    assert co.wait_workers_up(30)
    assert all(h.status == "healthy" for h in co.workers) and not old_pids & {h.pid for h in co.workers}
    assert co.availability.summary()["available"] == 400
    assert [len(h.owned) for h in co.workers] == [100, 100, 100, 100]
    assert fired and any(e["kind"] == "run_reset" for e in co.events)
    # a new run starts at the requested charge level
    co.start({"start_utc": EVENING, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.35},
             max_ticks=2, internal=True)
    assert np.all(co.read_hash_soc() == 0.35)
