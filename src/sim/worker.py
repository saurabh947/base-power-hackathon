"""Worker process: owns a cohort of batteries and applies the coordinator's command batches.

Run (the coordinator does this with subprocess.Popen, one OS process per worker):
    python -m src.sim.worker --worker-id 7 --epoch 42 [--redis-url URL] [--prefix P]

Per tick the worker:
  1. XREADs one msgpack batch from commands:{worker_id}: global battery indices,
     grid-side setpoints and availability flags (+ an optional 'load' section of
     batteries it just adopted, which it HMGETs lazily from their hashes first).
  2. Builds each battery's command id with make_command_id's formula (vectorized,
     identical ids) and dedupes per battery on last_command_id AND the tick fence
     (tick <= last_tick is a no-op). There is no global seen-set.
  3. Runs src.sim.battery.step on the fresh commands (vectorized numpy).
  4. Writes soc + last_command_id + last_tick for every applied battery AND the
     telemetry entry in ONE MULTI/EXEC, guarded by WATCH on lease:{worker_id}.
     If the coordinator has declared this worker DEAD it overwrote the lease, so
     the transaction aborts and a zombie can never clobber an adopter's state
     (dead_letter reason 'epoch_mismatch'; the worker then exits).
State is kept in-process between ticks (write-through; the preflight measured
this as the fastest layout). A failed write is retried up to 3 times, each time
re-reading the truth from Redis first, then dead-lettered as 'max_retries'.

A heartbeat thread SETs hb:{worker_id} every 2 s with a 6 s TTL while the lease is
still ours, and exits the process if the parent (coordinator) dies: macOS has no
PDEATHSIG, so it polls os.getppid().
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import threading
import time

import numpy as np
import redis

from src.sim import battery as physics
from src.sim.redis_layout import (
    DEFAULT_MAXLEN, F_AVAIL, F_CAP, F_CID, F_EFF, F_EPOCH, F_POW, F_RUN, F_SOC, F_TICK, NO_TICK,
    CommandIds, Keys, arr, connect, dead_letter_entry, pack, redis_url, resp, unpack, write_dead_letters,
)

LOAD_FIELDS = (F_SOC, F_CID, F_TICK, F_EPOCH, F_AVAIL, F_CAP, F_POW, F_EFF, F_RUN)
TRANSIENT = (redis.ConnectionError, redis.TimeoutError, OSError)

_MULTI = resp("MULTI")
_EXEC = resp("EXEC")
_UNWATCH = resp("UNWATCH")
_CID_HDR = b"$15\r\nlast_command_id\r\n$36\r\n"
_TICK_HDR = b"\r\n$9\r\nlast_tick\r\n"


def _log(msg: str) -> None:
    print(f"[worker {os.getpid()}] {msg}", file=sys.stderr, flush=True)


class Fenced(Exception):
    """Our lease was revoked: the coordinator declared this worker DEAD."""


class Worker:
    def __init__(
        self,
        worker_id: int,
        epoch: int,
        *,
        url: str | None = None,
        prefix: str = "",
        parent_pid: int | None = None,
        hb_interval_s: float = 2.0,
        hb_ttl_s: float = 6.0,
        block_ms: int = 1000,
        max_retries: int = 3,
        telemetry_maxlen: int = DEFAULT_MAXLEN["telemetry"],
    ) -> None:
        self.worker_id = int(worker_id)
        self.epoch = int(epoch)
        self.url = url or redis_url()
        self.keys = Keys(prefix)
        self.parent_pid = parent_pid
        self.hb_interval_s = float(hb_interval_s)
        self.hb_ttl_s = float(hb_ttl_s)
        self.block_ms = int(block_ms)
        self.max_retries = int(max_retries)
        self.telemetry_maxlen = int(telemetry_maxlen)
        self.r = connect(self.url)
        self.lease_value = str(self.epoch).encode()
        self._lease_key = self.keys.lease(self.worker_id)
        self._watch_get = resp("WATCH", self._lease_key) + resp("GET", self._lease_key)
        self._wconn = None
        self.stop = threading.Event()
        self.fenced = threading.Event()
        self.counters = {"applied": 0, "duplicate": 0, "stale": 0, "mismatch": 0, "not_owned": 0,
                         "invalid": 0, "max_retries": 0, "retries": 0}
        self.run_id: str | None = None
        self.dt_h = 300 / 3600
        self.soc_min, self.soc_max = physics.SOC_MIN, physics.SOC_MAX
        self._reset_state(0, None)
        self._stream_last = "0-0"

    # --- state ----------------------------------------------------------------------------

    def _reset_state(self, fleet_size: int, run_id: str | None) -> None:
        self.run_id = run_id
        self.fleet_size = int(fleet_size)
        self.ids_factory = CommandIds(run_id or "")
        self.posmap = np.full(self.fleet_size, -1, dtype=np.int64)
        self.g = np.empty(0, dtype=np.int32)
        self.bids: list[str] = []
        self.hdr: list[bytes] = []
        self.pref: list = []
        self.soc = np.empty(0)
        self.cap = np.empty(0)
        self.pow = np.empty(0)
        self.eff = np.empty(0)
        self.avail = np.empty(0, dtype=bool)
        self.last_tick = np.empty(0, dtype=np.int64)
        self.last_id = np.empty(0, dtype="S36")
        self.dirty: set[int] = set()
        self._pref_cache: tuple[bytes, list] | None = None

    @property
    def battery_count(self) -> int:
        return len(self.bids)

    # --- main loop ------------------------------------------------------------------------

    def run(self) -> int:
        hb = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        hb.start()
        stream = self.keys.commands(self.worker_id)
        _log(f"worker {self.worker_id} epoch {self.epoch} up, reading {stream}")
        try:
            while not self.stop.is_set():
                if self.fenced.is_set():
                    _log(f"worker {self.worker_id}: lease revoked (declared dead); exiting")
                    return 3
                self.serve_once(self.block_ms)
        except Fenced:
            _log(f"worker {self.worker_id}: fenced out by a newer owner epoch; exiting")
            return 3
        return 0

    def serve_once(self, block_ms: int | None) -> int:
        """Read and handle what is waiting in our command stream. Returns messages handled."""
        stream = self.keys.commands(self.worker_id)
        try:
            resp_ = self.r.xread({stream: self._stream_last}, block=block_ms, count=16)
        except TRANSIENT as exc:
            _log(f"xread failed: {exc!r}")
            time.sleep(0.2)
            return 0
        n = 0
        for _name, entries in resp_ or []:
            for msg_id, fields in entries:
                self._stream_last = msg_id
                self.handle(unpack(fields[b"m"]))
                n += 1
                if self.stop.is_set():
                    return n
        return n

    def handle(self, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "tick":
            self.on_tick(msg)
        elif kind == "load":
            self.on_load(msg)
        elif kind == "stop":
            self.stop.set()

    # --- load (run start and adoption) -------------------------------------------------------

    def on_load(self, msg: dict) -> None:
        """Run start: forget the previous run, then load (and initialise) our cohort."""
        self._reset_state(msg["n"], msg["run"])
        self.dt_h = float(msg.get("dt_h", self.dt_h))
        self.soc_min = float(msg.get("soc_min", self.soc_min))
        self.soc_max = float(msg.get("soc_max", self.soc_max))
        loaded, skipped = self._load(msg["load"])
        self.r.xadd(self.keys.telemetry, {"m": pack({
            "t": "ready", "w": self.worker_id, "run": self.run_id, "e": self.epoch,
            "n": loaded, "skipped": skipped, "pid": os.getpid(),
        })}, maxlen=self.telemetry_maxlen, approximate=True)

    def _load(self, section: dict) -> tuple[int, int]:
        """HMGET batteries from their hashes (the source of truth) and add them to our state.

        section = {idx: int32 bytes, ids: [str], init: uint8 bytes, soc0: float}. init=1 starts
        the battery fresh for this run (soc0, empty fence); init=0 adopts its stored state.
        """
        idx = arr(section["idx"], np.int32)
        ids = list(section["ids"])
        init = arr(section["init"], np.uint8).astype(bool) if section.get("init") is not None \
            else np.zeros(len(idx), dtype=bool)
        soc0 = section.get("soc0")
        if len(idx) == 0:
            return 0, 0
        pipe = self.r.pipeline(transaction=False)
        for bid in ids:
            pipe.hmget(self.keys.battery(bid), LOAD_FIELDS)
        rows = pipe.execute()
        keep, soc, cid, tick, avail, cap, pw, eff, fresh = [], [], [], [], [], [], [], [], []
        skipped = 0
        for j, row in enumerate(rows):
            if row[5] is None:  # no hash: nothing to adopt
                skipped += 1
                self.counters["not_owned"] += 1
                continue
            owner_epoch = int(row[3]) if row[3] is not None else None
            if owner_epoch != self.epoch:  # assigned to someone else: never touch it
                skipped += 1
                self.counters["mismatch"] += 1
                continue
            is_init = bool(init[j]) or (row[8] or b"").decode() != self.run_id
            keep.append(j)
            avail.append(row[4] in (b"1", b"True", b"true"))
            cap.append(float(row[5]))
            pw.append(float(row[6]))
            eff.append(float(row[7]))
            if is_init:
                soc.append(float(soc0))
                cid.append(b"")
                tick.append(NO_TICK)
            else:
                soc.append(float(row[0]))
                cid.append(row[1] or b"")
                tick.append(int(float(row[2])) if row[2] not in (None, b"") else NO_TICK)
            fresh.append(is_init)
        if not keep:
            return 0, skipped
        g = idx[keep]
        new_bids = [ids[j] for j in keep]
        known = self.posmap[g] >= 0
        if known.any():  # re-load of batteries we already hold: update in place
            for j in np.flatnonzero(known):
                p = int(self.posmap[g[j]])
                self.soc[p], self.last_id[p], self.last_tick[p] = soc[j], cid[j], tick[j]
                self.avail[p], self.cap[p], self.pow[p], self.eff[p] = avail[j], cap[j], pw[j], eff[j]
        add = np.flatnonzero(~known)
        if add.size:
            start = len(self.bids)
            self.g = np.concatenate([self.g, g[add]])
            add_bids = [new_bids[j] for j in add]
            self.bids.extend(add_bids)
            for bid in add_bids:
                key = self.keys.battery(bid).encode()
                self.hdr.append(b"*8\r\n$4\r\nHSET\r\n$%d\r\n%s\r\n$3\r\nsoc\r\n" % (len(key), key))
            self.pref.extend(self.ids_factory.prefixes(add_bids))
            take = lambda xs, dt: np.array([xs[j] for j in add], dtype=dt)  # noqa: E731
            self.soc = np.concatenate([self.soc, take(soc, np.float64)])
            self.cap = np.concatenate([self.cap, take(cap, np.float64)])
            self.pow = np.concatenate([self.pow, take(pw, np.float64)])
            self.eff = np.concatenate([self.eff, take(eff, np.float64)])
            self.avail = np.concatenate([self.avail, take(avail, bool)])
            self.last_tick = np.concatenate([self.last_tick, take(tick, np.int64)])
            self.last_id = np.concatenate([self.last_id, take(cid, "S36")])
            self.posmap[g[add]] = np.arange(start, start + add.size)
            self._pref_cache = None
        init_pos = [int(self.posmap[g[j]]) for j in range(len(keep)) if fresh[j]]
        if init_pos:
            self._write_init(init_pos)
        return len(keep), skipped

    def _write_init(self, positions: list[int]) -> None:
        """Fresh run state (soc0, empty fence, run_id) for these batteries, fenced by our lease."""
        run = (self.run_id or "").encode()
        parts = []
        for p in positions:
            soc_b = repr(float(self.soc[p])).encode()
            key = self.keys.battery(self.bids[p])
            parts.append(resp("HSET", key, "soc", soc_b, "last_command_id", b"", "last_tick", str(NO_TICK), "run_id", run))
        self._transact(parts)

    def _reload(self, positions) -> None:
        """Re-read soc and the fence from Redis (after an ambiguous write failure)."""
        positions = [int(p) for p in positions]
        if not positions:
            return
        pipe = self.r.pipeline(transaction=False)
        for p in positions:
            pipe.hmget(self.keys.battery(self.bids[p]), (F_SOC, F_CID, F_TICK))
        for p, row in zip(positions, pipe.execute()):
            if row[0] is not None:
                self.soc[p] = float(row[0])
                self.last_id[p] = row[1] or b""
                self.last_tick[p] = int(float(row[2])) if row[2] not in (None, b"") else NO_TICK
        self.dirty.difference_update(positions)

    # --- ticks ----------------------------------------------------------------------------

    def on_tick(self, msg: dict) -> None:
        run = msg["run"]
        k, ts = int(msg["k"]), int(msg["ts"])
        idx = arr(msg["idx"], np.int32)
        sp = arr(msg["sp"], np.float64)
        av = arr(msg["av"], np.uint8).astype(bool)
        if run != self.run_id:  # never loaded for this run: nothing we can apply
            self._dead_letter_batch(idx, sp, ts, "not_loaded", run)
            return
        if msg.get("load"):
            self._load(msg["load"])
        pos = np.full(len(idx), -1, dtype=np.int64)
        inside = (idx >= 0) & (idx < self.fleet_size)
        pos[inside] = self.posmap[idx[inside]]
        known = pos >= 0
        finite = np.isfinite(sp)
        usable = known & finite
        if (~known).any():
            self.counters["not_owned"] += int((~known).sum())
            self._dead_letter_batch(idx[~known], sp[~known], ts, "not_owned", run)
        if (known & ~finite).any():
            self.counters["invalid"] += int((known & ~finite).sum())
            bad = known & ~finite
            self._dead_letter_batch(idx[bad], sp[bad], ts, "invalid_setpoint", run)
        p = pos[usable]
        self.avail[p] = av[usable]
        setpoints = sp[usable]
        pkey = p.tobytes()
        if self._pref_cache is None or self._pref_cache[0] != pkey:
            self._pref_cache = (pkey, [self.pref[i] for i in p.tolist()])
        ids = CommandIds.generate(self._pref_cache[1], ts)

        started = time.perf_counter()
        for attempt in range(1, self.max_retries + 1):
            try:
                if self.dirty:
                    self._reload(sorted(self.dirty))
                outcome = self._apply(k, ts, idx, usable, p, setpoints, ids, started)
                break
            except Fenced:
                self._dead_letter_positions(p, setpoints, ids, ts, "epoch_mismatch", run)
                self.counters["mismatch"] += len(p)
                self.fenced.set()
                raise
            except TRANSIENT as exc:
                self.counters["retries"] += 1
                _log(f"tick {k}: write attempt {attempt} failed: {exc!r}")
                self._drop_conn()
                self.dirty.update(p.tolist())  # the EXEC may or may not have landed: re-read the truth
                time.sleep(0.01 * attempt)
        else:
            self.counters["max_retries"] += len(p)
            try:
                self._dead_letter_positions(p, setpoints, ids, ts, "max_retries", run)
            except TRANSIENT as exc:
                _log(f"could not dead-letter tick {k}: {exc!r}")
            return
        if outcome["stale"]:
            sel = outcome["stale_mask"]
            self._dead_letter_positions(p[sel], setpoints[sel], ids[sel], ts, "stale", run)

    def _apply(self, k, ts, idx, usable, p, setpoints, ids, started) -> dict:
        dup = ids == self.last_id[p]
        fresh = (ts > self.last_tick[p]) & ~dup
        stale = ~fresh & ~dup
        fp = p[fresh]
        new_soc, delivered = physics.step(
            self.soc[fp], self.cap[fp], self.pow[fp], self.eff[fp], self.avail[fp], setpoints[fresh],
            self.dt_h, self.soc_min, self.soc_max,
        )
        soc_out = np.full(len(idx), np.nan)
        kw_out = np.zeros(len(idx))
        soc_now = self.soc[p].copy()
        soc_now[fresh] = new_soc
        soc_out[usable] = soc_now
        kw_used = np.zeros(len(p))
        kw_used[fresh] = delivered
        kw_out[usable] = kw_used
        n = {"applied": int(fresh.sum()), "dup": int(dup.sum()), "stale": int(stale.sum())}
        payload = pack({
            "t": "tick", "w": self.worker_id, "run": self.run_id, "k": k, "ts": ts, "e": self.epoch,
            "idx": idx.tobytes(), "soc": soc_out.tobytes(), "kw": kw_out.tobytes(), "n": n,
            "ms": round((time.perf_counter() - started) * 1000.0, 3), "pid": os.getpid(),
        })
        self._commit_tick(fp, new_soc, ids[fresh], ts, payload)
        # committed: only now does in-process state move (write-through)
        self.soc[fp] = new_soc
        self.last_tick[fp] = ts
        self.last_id[fp] = ids[fresh]
        self.counters["applied"] += n["applied"]
        self.counters["duplicate"] += n["dup"]
        self.counters["stale"] += n["stale"]
        return {"stale": n["stale"], "stale_mask": stale}

    def _commit_tick(self, fp: np.ndarray, new_soc: np.ndarray, ids_fresh: np.ndarray, ts: int, payload: bytes) -> None:
        """soc + fence for each applied battery and the telemetry entry, atomically, under our lease."""
        tb = str(ts).encode()
        tick_part = b"$%d\r\n%s\r\n" % (len(tb), tb)
        socs = new_soc.tolist()
        idl = ids_fresh.tolist()
        hdr = self.hdr
        parts = []
        append = parts.append
        for j, i in enumerate(fp.tolist()):
            sb = repr(socs[j]).encode()
            append(hdr[i])
            append(b"$%d\r\n%s\r\n" % (len(sb), sb))
            append(_CID_HDR)
            append(idl[j])
            append(_TICK_HDR)
            append(tick_part)
        cmds = [b"".join(parts)] if parts else []
        cmds.append(resp("XADD", self.keys.telemetry, "MAXLEN", "~", self.telemetry_maxlen, "*", "m", payload))
        self._transact(cmds, n_commands=len(fp) + 1)

    def _transact(self, commands: list[bytes], n_commands: int | None = None) -> list:
        """WATCH lease; check it is still ours; MULTI; commands; EXEC. Raises Fenced if not."""
        conn = self._conn()
        try:
            conn.send_packed_command([self._watch_get])
            conn.read_response()
            lease = conn.read_response()
            if lease != self.lease_value:
                conn.send_packed_command([_UNWATCH])
                conn.read_response()
                raise Fenced(lease)
            conn.send_packed_command([_MULTI, *commands, _EXEC])
            conn.read_response()
            for _ in range(len(commands) if n_commands is None else n_commands):
                conn.read_response()
            result = conn.read_response()
        except Fenced:
            raise
        except BaseException:
            self._drop_conn()
            raise
        if result is None:  # the lease changed between WATCH and EXEC
            raise Fenced(None)
        for item in result:
            if isinstance(item, Exception):
                raise redis.ConnectionError(f"command failed inside EXEC: {item!r}")
        return result

    def _conn(self):
        if self._wconn is None:
            self._wconn = self.r.connection_pool.make_connection()
            self._wconn.connect()
        return self._wconn

    def _drop_conn(self) -> None:
        if self._wconn is not None:
            try:
                self._wconn.disconnect()
            except Exception:
                pass
            self._wconn = None

    # --- dead letters ---------------------------------------------------------------------

    def _dead_letter_positions(self, p, setpoints, ids, ts, reason, run) -> None:
        entries = [
            dead_letter_entry(command_id=ids[j].decode(), battery_id=self.bids[int(p[j])], worker_id=self.worker_id,
                              tick_ts=ts, reason=reason, run_id=run, setpoint_kw=float(setpoints[j]))
            for j in range(len(p))
        ]
        write_dead_letters(self.r, self.keys, entries)

    def _dead_letter_batch(self, idx, sp, ts, reason, run) -> None:
        """Dead-letter commands for batteries we hold no state for (ids come from the published layout)."""
        try:
            raw = self.r.get(self.keys.layout)
            layout = json.loads(raw) if raw else None
        except TRANSIENT:
            layout = None
        from src.sim.redis_layout import battery_id as make_bid

        entries = []
        ids_factory = CommandIds(run)
        for j, g in enumerate(np.asarray(idx).tolist()):
            bid = str(g)
            if layout:
                off = 0
                for loc, n in layout["locations"]:
                    if g < off + n:
                        bid = make_bid(loc, g - off)
                        break
                    off += n
            cid = ids_factory.ids([bid], ts)[0]
            sp_j = float(sp[j])
            entries.append(dead_letter_entry(command_id=cid, battery_id=bid, worker_id=self.worker_id, tick_ts=ts,
                                             reason=reason, run_id=run,
                                             setpoint_kw=sp_j if math.isfinite(sp_j) else None))
        write_dead_letters(self.r, self.keys, entries)

    # --- heartbeat --------------------------------------------------------------------------

    def _parent_alive(self) -> bool:
        return self.parent_pid is None or os.getppid() == self.parent_pid

    def _heartbeat_loop(self) -> None:
        client = connect(self.url)
        hb_key = self.keys.hb(self.worker_id)
        next_beat = 0.0
        while not self.stop.is_set():
            if not self._parent_alive():
                _log(f"worker {self.worker_id}: parent {self.parent_pid} is gone; exiting")
                os._exit(0)
            now = time.monotonic()
            if now >= next_beat:
                try:
                    if client.get(self._lease_key) != self.lease_value:
                        self.fenced.set()  # declared dead: stop beating, the main loop exits
                        return
                    client.set(hb_key, json.dumps({"pid": os.getpid(), "epoch": self.epoch, "t": time.time()}),
                               px=max(1, int(self.hb_ttl_s * 1000)))
                except TRANSIENT:
                    pass
                next_beat = now + self.hb_interval_s
            self.stop.wait(min(0.25, self.hb_interval_s))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.sim.worker", description="fleet-sim worker process")
    ap.add_argument("--worker-id", type=int, required=True)
    ap.add_argument("--epoch", type=int, required=True)
    ap.add_argument("--redis-url", default=None)
    ap.add_argument("--prefix", default=os.environ.get("FLEET_REDIS_PREFIX", ""))
    ap.add_argument("--parent-pid", type=int, default=None, help="exit when this process is no longer our parent")
    ap.add_argument("--hb-interval", type=float, default=2.0)
    ap.add_argument("--hb-ttl", type=float, default=6.0)
    ap.add_argument("--block-ms", type=int, default=1000)
    args = ap.parse_args(argv)
    # Ctrl+C reaches the whole process group; the coordinator stops us cleanly (and SIGTERM/SIGKILL still work)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    worker = Worker(args.worker_id, args.epoch, url=args.redis_url, prefix=args.prefix,
                    parent_pid=args.parent_pid if args.parent_pid is not None else os.getppid(),
                    hb_interval_s=args.hb_interval, hb_ttl_s=args.hb_ttl, block_ms=args.block_ms)
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
