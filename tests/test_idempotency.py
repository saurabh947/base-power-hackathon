"""Duplicate redelivery never double-applies a command (spec M3), including across adoption.

Real worker processes (python -m src.sim.worker) are driven through Redis with hand-built
batches whose command ids come from make_command_id's formula. The battery hashes are
then compared, exactly, with ONE application of src.sim.battery.step per tick.
"""
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import numpy as np
import pytest

from src.config import REPO_ROOT, load_config
from src.core.ids import make_command_id
from src.sim import battery as physics
from src.sim.redis_layout import (
    F_AVAIL, F_CAP, F_CID, F_EFF, F_EPOCH, F_ID, F_IDX, F_LOC, F_OWNER, F_POW, F_RUN, F_SOC, F_TICK, NO_TICK,
    FleetLayout, Keys, connect, f64, i32, pack, reachable, redis_url, u8, unpack,
)
from src.sim.worker import Worker

pytestmark = pytest.mark.redis
UTC = timezone.utc
T0 = int(datetime(2025, 5, 16, 21, 0, tzinfo=UTC).timestamp())
DT_H = 300 / 3600
RUN = "idem-run"


@pytest.fixture
def rig():
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    rig = Rig(url, f"test:idem:{uuid.uuid4().hex[:8]}:")
    yield rig
    rig.close()


class Rig:
    """A 40-battery fleet in Redis plus helpers to drive worker processes by hand."""

    def __init__(self, url: str, prefix: str) -> None:
        self.url, self.prefix = url, prefix
        self.r = connect(url)
        self.keys = Keys(prefix)
        self.layout = FleetLayout.build(load_config(), size=40, workers=2)
        self.procs: list[subprocess.Popen] = []
        self.tele_last = "0-0"
        self.r.set(self.keys.layout, json.dumps(self.layout.public()))

    def seed(self, owners: dict[int, tuple[int, int]], soc0: float = 0.5) -> None:
        """owners: global index -> (worker_id, epoch)."""
        pipe = self.r.pipeline(transaction=False)
        for g, (w, epoch) in owners.items():
            pipe.hset(self.keys.battery(self.layout.ids[g]), mapping={
                F_SOC: repr(soc0), F_CID: "", F_TICK: NO_TICK, F_EPOCH: epoch, F_AVAIL: 1, F_OWNER: w, F_RUN: "",
                F_ID: self.layout.ids[g], F_LOC: "x", F_IDX: g, F_CAP: repr(float(self.layout.capacity_kwh[g])),
                F_POW: repr(float(self.layout.power_kw[g])), F_EFF: repr(float(self.layout.efficiency[g]))})
        pipe.execute()

    def spawn(self, w: int, epoch: int) -> subprocess.Popen:
        self.r.set(self.keys.lease(w), epoch)
        p = subprocess.Popen([sys.executable, "-m", "src.sim.worker", "--worker-id", str(w), "--epoch", str(epoch),
                              "--redis-url", self.url, "--prefix", self.prefix, "--parent-pid", str(os.getpid()),
                              "--block-ms", "200"], cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def send(self, w: int, msg: dict) -> None:
        self.r.xadd(self.keys.commands(w), {"m": pack(msg)})

    def load(self, w: int, idx, soc0: float = 0.5) -> None:
        idx = np.asarray(idx, dtype=np.int32)
        self.send(w, {"t": "load", "run": RUN, "n": self.layout.size, "dt_h": DT_H, "soc_min": 0.2, "soc_max": 1.0,
                      "load": {"idx": i32(idx), "ids": [self.layout.ids[g] for g in idx], "init": u8(np.ones(len(idx))),
                               "soc0": soc0}})

    def tick(self, w: int, k: int, idx, sp, *, adopt=None) -> None:
        idx = np.asarray(idx, dtype=np.int32)
        msg = {"t": "tick", "run": RUN, "k": k, "ts": T0 + 300 * k, "idx": i32(idx), "sp": f64(sp),
               "av": u8(np.ones(len(idx)))}
        if adopt is not None:
            adopt = np.asarray(adopt, dtype=np.int32)
            msg["load"] = {"idx": i32(adopt), "ids": [self.layout.ids[g] for g in adopt],
                           "init": u8(np.zeros(len(adopt))), "soc0": 0.5}
        self.send(w, msg)

    def telemetry(self, want: int, timeout: float = 15.0) -> list[dict]:
        """Wait until `want` more telemetry entries arrive; return them."""
        out, deadline = [], time.monotonic() + timeout
        while len(out) < want and time.monotonic() < deadline:
            resp = self.r.xread({self.keys.telemetry: self.tele_last}, block=200)
            for _name, entries in resp or []:
                for msg_id, fields in entries:
                    self.tele_last = msg_id
                    out.append(unpack(fields[b"m"]))
        assert len(out) >= want, f"only {len(out)} of {want} telemetry entries arrived"
        return out

    def soc(self, idx) -> np.ndarray:
        pipe = self.r.pipeline(transaction=False)
        for g in idx:
            pipe.hget(self.keys.battery(self.layout.ids[g]), F_SOC)
        return np.array([float(x) for x in pipe.execute()])

    def fence(self, g: int) -> tuple[str, int]:
        cid, tick = self.r.hmget(self.keys.battery(self.layout.ids[g]), (F_CID, F_TICK))
        return cid.decode(), int(float(tick))

    def dead_letters(self) -> list[dict]:
        return [json.loads(f[b"j"]) for _i, f in self.r.xrange(self.keys.dead_letter)]

    def expected(self, idx, soc0: float, setpoints: list) -> np.ndarray:
        """One application per tick of src.sim.battery physics."""
        idx = np.asarray(idx)
        soc = np.full(len(idx), soc0)
        for sp in setpoints:
            soc, _ = physics.step(soc, self.layout.capacity_kwh[idx], self.layout.power_kw[idx],
                                  self.layout.efficiency[idx], np.ones(len(idx), bool), sp, DT_H)
        return soc

    def close(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)
        keys = list(self.r.scan_iter(match=self.prefix + "*", count=1000))
        if keys:
            self.r.unlink(*keys)


def _setpoints(rig: Rig, idx, k: int) -> np.ndarray:
    """Deterministic test setpoints (synthetic, labeled: not prices)."""
    idx = np.asarray(idx)
    sign = -1.0 if k % 2 else 1.0
    return sign * rig.layout.power_kw[idx] * (0.3 + 0.1 * (k % 3))


def test_duplicate_and_stale_redelivery_never_double_applies(rig):
    idx = list(range(20))
    rig.seed({g: (0, 11) for g in idx})
    rig.spawn(0, 11)
    rig.load(0, idx)
    ready = rig.telemetry(1)[0]
    assert ready["t"] == "ready" and ready["n"] == 20
    sp1, sp2 = _setpoints(rig, idx, 1), _setpoints(rig, idx, 2)
    rig.tick(0, 1, idx, sp1)
    rig.tick(0, 1, idx, sp1)  # redelivered: same command ids
    rig.tick(0, 2, idx, sp2)
    rig.tick(0, 2, idx, sp2)  # redelivered again
    rig.tick(0, 1, idx, sp1 * 0 + 5.0)  # an older tick shows up late with the same ids
    reports = rig.telemetry(5)
    applied = [r["n"]["applied"] for r in reports]
    dups = [r["n"]["dup"] for r in reports]
    stale = [r["n"]["stale"] for r in reports]
    assert applied == [20, 0, 20, 0, 0] and dups == [0, 20, 0, 20, 0] and stale == [0, 0, 0, 0, 20]
    assert np.array_equal(rig.soc(idx), rig.expected(idx, 0.5, [sp1, sp2]))  # exactly one application each
    for g in idx:
        assert rig.fence(g) == (make_command_id(RUN, rig.layout.ids[g], datetime.fromtimestamp(T0 + 600, UTC)),
                                T0 + 600)
    # the late tick-1 batch carried the same ids as the applied ones, but its tick is behind the fence
    assert {d["reason"] for d in rig.dead_letters()} == {"stale"}


def test_redelivery_across_adoption_is_a_noop(rig):
    a_idx, b_idx = list(range(20)), list(range(20, 40))
    rig.seed({**{g: (0, 21) for g in a_idx}, **{g: (1, 22) for g in b_idx}})
    worker_a, _ = rig.spawn(0, 21), rig.spawn(1, 22)
    rig.load(0, a_idx)
    rig.load(1, b_idx)
    rig.telemetry(2)
    sp = {k: _setpoints(rig, list(range(40)), k) for k in (1, 2, 3)}
    for k in (1, 2):
        rig.tick(0, k, a_idx, sp[k][:20])
        rig.tick(1, k, b_idx, sp[k][20:])
    rig.telemetry(4)
    worker_a.send_signal(signal.SIGKILL)
    worker_a.wait(timeout=5)
    # what the coordinator does on DEAD: fence the dead worker, then hand its cohort over
    rig.r.set(rig.keys.lease(0), "dead:21")
    for g in a_idx:
        rig.r.hset(rig.keys.battery(rig.layout.ids[g]), mapping={F_OWNER: 1, F_EPOCH: 22})
    # the adopter gets the dead worker's tick-2 commands again (same ids), then tick 3 for everyone
    rig.tick(1, 2, a_idx, sp[2][:20], adopt=a_idx)
    rig.tick(1, 3, list(range(40)), sp[3])
    reports = rig.telemetry(2)
    assert reports[0]["n"] == {"applied": 0, "dup": 20, "stale": 0}
    assert reports[1]["n"] == {"applied": 40, "dup": 0, "stale": 0}
    everyone = list(range(40))
    assert np.array_equal(rig.soc(everyone), rig.expected(everyone, 0.5, [sp[1], sp[2], sp[3]]))
    assert not rig.dead_letters()


def test_zombie_writes_are_fenced_out(rig):
    idx = list(range(10))
    rig.seed({g: (0, 31) for g in idx})
    zombie = rig.spawn(0, 31)
    rig.load(0, idx)
    rig.telemetry(1)
    sp1 = _setpoints(rig, idx, 1)
    rig.tick(0, 1, idx, sp1)
    rig.telemetry(1)
    os.kill(zombie.pid, signal.SIGSTOP)  # frozen, not dead: a zombie in the making
    try:
        rig.tick(0, 2, idx, _setpoints(rig, idx, 2))  # waits in its stream
        rig.r.set(rig.keys.lease(0), "dead:31")  # the coordinator declares it DEAD
        before = rig.soc(idx)
    finally:
        os.kill(zombie.pid, signal.SIGCONT)
    assert zombie.wait(timeout=10) == 3  # it noticed the fence and left
    assert np.array_equal(rig.soc(idx), before)  # and wrote nothing
    assert np.array_equal(before, rig.expected(idx, 0.5, [sp1]))
    reasons = [d["reason"] for d in rig.dead_letters()]
    assert reasons == ["epoch_mismatch"] * 10


class _FlakyConn:
    """Wraps the worker's write connection: the first `fail` EXEC batches raise ConnectionError."""

    def __init__(self, inner, fail: int, after_send: bool) -> None:
        self.inner, self.fail, self.after_send = inner, fail, after_send

    def send_packed_command(self, cmds, *a, **kw):
        if self.fail and len(cmds) > 1:  # the MULTI ... EXEC batch
            self.fail -= 1
            if self.after_send:  # the write lands but its reply is lost: the worst case
                self.inner.send_packed_command(cmds, *a, **kw)
            import redis

            raise redis.ConnectionError("injected")
        return self.inner.send_packed_command(cmds, *a, **kw)

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.mark.parametrize("fail, after_send, outcome", [
    (2, False, "applied"),     # two lost writes, third attempt lands
    (2, True, "applied"),      # the first write DID land, its reply was lost: retries must not re-apply
    (3, False, "max_retries"),
])
def test_write_retries_apply_at_most_once(rig, fail, after_send, outcome):
    idx = list(range(8))
    rig.seed({g: (0, 41) for g in idx})
    rig.r.set(rig.keys.lease(0), 41)
    w = Worker(0, 41, url=rig.url, prefix=rig.prefix, block_ms=100)
    rig.load(0, idx)
    w.serve_once(None)
    rig.telemetry(1)
    sp1 = _setpoints(rig, idx, 1)
    rig.tick(0, 1, idx, sp1)
    real = w._conn()
    flaky = _FlakyConn(real, fail, after_send)
    w._wconn = flaky
    orig_drop = w._drop_conn

    def drop():  # a real reconnect after each failure, but keep failing while the budget lasts
        orig_drop()
        if flaky.fail:
            flaky.inner = w.r.connection_pool.make_connection()
            flaky.inner.connect()
            w._wconn = flaky

    w._drop_conn = drop
    w.serve_once(None)
    if outcome == "applied":
        assert np.array_equal(rig.soc(idx), rig.expected(idx, 0.5, [sp1]))
        assert w.counters["retries"] == 2 and w.counters["max_retries"] == 0
        assert not [d for d in rig.dead_letters() if d["reason"] == "max_retries"]
    else:
        assert np.array_equal(rig.soc(idx), np.full(len(idx), 0.5))
        assert [d["reason"] for d in rig.dead_letters()] == ["max_retries"] * len(idx)
    # the next tick starts from the truth in Redis either way
    w._drop_conn = orig_drop
    w._wconn = None
    sp2 = _setpoints(rig, idx, 2)
    rig.tick(0, 2, idx, sp2)
    w.serve_once(None)
    applied = [sp1, sp2] if outcome == "applied" else [sp2]
    assert np.array_equal(rig.soc(idx), rig.expected(idx, 0.5, applied))
