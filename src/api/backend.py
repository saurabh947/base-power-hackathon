"""What the API reads from and sends to the coordinator, all through Redis (sync redis-py;
FastAPI runs these calls in its threadpool)."""
from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Mapping

import numpy as np

from src.api.layout import battery_lonlat, layout_message
from src.sim import battery_status as bstatus
from src.sim import control as ctl
from src.sim import geo
from src.sim.redis_layout import Keys, connect, layout_index, location_counts, now_utc_iso, redis_prefix, redis_url

EMPTY_TOTALS = {"revenue_usd": 0.0, "sold_kwh": 0.0, "bought_kwh": 0.0, "ticks": 0}


def idle_battery_status(fleet_size: int) -> str:
    """battery_status with no run and nothing offline: every battery waiting (1), base64."""
    return base64.b64encode(bstatus.pack(bstatus.idle_codes(np.zeros(int(fleet_size), dtype=bool)))).decode("ascii")


def empty_totals(locations) -> dict:
    """docs/API.md Totals before any run: zeros, and zeros per city."""
    return {**EMPTY_TOTALS, "by_location": {loc: {"revenue_usd": 0.0, "sold_kwh": 0.0, "bought_kwh": 0.0}
                                            for loc in locations}}


class Backend:
    def __init__(self, cfg, raw: Mapping, *, url: str | None = None, prefix: str | None = None) -> None:
        self.cfg = cfg
        self.raw = raw
        self.url = url or redis_url()
        self.keys = Keys(redis_prefix() if prefix is None else prefix)
        self.r = connect(self.url, socket_timeout=8.0)
        self.control = ctl.ControlClient(self.r, self.keys)

    # --- coordinator state -------------------------------------------------------------------

    def idle_snapshot(self) -> dict:
        settings = ctl.default_settings(self.cfg, self.raw)
        split = location_counts(self.cfg.fleet.size, self.cfg.fleet.locations)
        return {
            "run_id": None, "status": "idle", "settings": settings, "sim_time_utc": None,
            "speed_tps": settings["speed_tps"], "totals": empty_totals(self.cfg.fleet.locations), "workers": [],
            "availability": {"available": self.cfg.fleet.size, "total": self.cfg.fleet.size,
                             "by_location": {loc: {"available": n, "total": n} for loc, n in split},
                             "offline_workers": []},
        }

    def snapshot(self) -> dict:
        snap = self.coordinator_snapshot()
        return snap if snap is not None else self.idle_snapshot()

    def coordinator_snapshot(self) -> dict | None:
        """The coordinator's own snapshot, or None before it has published one (cold start)."""
        raw = self.r.get(self.keys.snapshot)
        return json.loads(raw) if raw else None

    def layout(self) -> dict | None:
        raw = self.r.get(self.keys.layout)
        return json.loads(raw) if raw else None

    def fleet_layout(self) -> dict:
        """GET /fleet/layout: the running fleet's size and seed (config.yaml's until the simulator is up)."""
        try:
            running = self.layout()
        except Exception:  # Redis not reachable yet: the configured fleet is the one that will run
            running = None
        size = int(running["size"]) if running else int(self.cfg.fleet.size)
        seed = int(running.get("seed", self.cfg.seed)) if running else int(self.cfg.seed)
        return layout_message(size, self.cfg.fleet.locations, seed)

    def battery_status(self, fleet_size: int | None = None) -> str:
        """docs/API.md battery_status: base64 of the packed 4-bit codes the coordinator wrote at the latest tick.
        Before the coordinator wrote any: every battery waiting (1)."""
        raw = self.r.get(self.keys.battery_status)
        if not raw:
            return idle_battery_status(int(fleet_size or self.cfg.fleet.size))
        return base64.b64encode(raw).decode("ascii")

    def status(self) -> str:
        try:
            return self.snapshot().get("status", "idle")
        except Exception:
            return "idle"

    # --- streams ---------------------------------------------------------------------------

    @staticmethod
    def _sid(x) -> str:
        return x.decode() if isinstance(x, bytes) else str(x)

    def history(self, run_id: str | None, n: int) -> tuple[list[dict], str]:
        rows = self.r.xrevrange(self.keys.ticks, "+", "-", count=n)
        cursor = self._sid(rows[0][0]) if rows else self._stream_last_id(self.keys.ticks)
        ticks = [json.loads(f[b"j"]) for _id, f in reversed(rows) if run_id is None or f.get(b"run", b"").decode() == run_id]
        return ticks, cursor

    def ticks_since(self, run_id: str | None, cursor: str, limit: int = 5000) -> tuple[list[dict], str]:
        rows = self.r.xrange(self.keys.ticks, f"({cursor}", "+", count=limit)
        if rows:
            cursor = self._sid(rows[-1][0])
        return [json.loads(f[b"j"]) for _id, f in rows if run_id is None or f.get(b"run", b"").decode() == run_id], cursor

    def events_recent(self, n: int) -> tuple[list[dict], str]:
        rows = self.r.xrevrange(self.keys.events, "+", "-", count=n)
        cursor = self._sid(rows[0][0]) if rows else self._stream_last_id(self.keys.events)
        return [self._event(i, f) for i, f in reversed(rows)], cursor

    def events_since(self, cursor: str, limit: int = 500) -> tuple[list[dict], str]:
        rows = self.r.xrange(self.keys.events, f"({cursor}", "+", count=limit)
        if rows:
            cursor = self._sid(rows[-1][0])
        return [self._event(i, f) for i, f in rows], cursor

    def _event(self, msg_id, fields) -> dict:
        return {"id": self._sid(msg_id), **json.loads(fields[b"j"])}

    def _stream_last_id(self, key: str) -> str:
        try:
            info = self.r.xinfo_stream(key)
            return self._sid(info.get("last-generated-id", "0-0"))
        except Exception:  # no stream yet
            return "0-0"

    def failed_commands(self) -> dict:
        pipe = self.r.pipeline(transaction=False)
        pipe.get(self.keys.dead_letter_count)
        pipe.xrevrange(self.keys.dead_letter, "+", "-", count=10)
        count, rows = pipe.execute()
        recent = []
        for _id, f in rows:
            e = json.loads(f[b"j"])
            recent.append({"battery_id": e.get("battery_id"), "worker_id": e.get("worker_id"),
                           "reason": e.get("reason"), "tick_utc": e.get("tick_utc")})
        return {"count": int(count or 0), "recent": recent}

    def emit(self, kind: str, severity: str, message: str, *, sim_ts=None, detail=None, once_key: str | None = None) -> None:
        """Events the API itself originates (forecast_issued, backtest_done), deduped by once_key."""
        if once_key and not self.r.set(self.keys.prefix + "evt:" + once_key, 1, nx=True, ex=86400 * 7):
            return
        ctl.emit_event(self.r, self.keys, ctl.make_event(kind, severity, message, sim_ts=sim_ts, detail=detail))

    # --- workers and batteries -----------------------------------------------------------------

    def registry(self) -> dict[int, dict]:
        return {int(k): json.loads(v) for k, v in self.r.hgetall(self.keys.workers).items()}

    def coordinator_pid(self) -> int | None:
        raw = self.r.get(self.keys.coordinator)
        return json.loads(raw).get("pid") if raw else None

    def redis_pid(self) -> int | None:
        try:
            return int(self.r.info("server").get("process_id"))
        except Exception:
            return None

    def battery(self, bid: str) -> dict:
        layout = self.layout()
        if layout is None:
            raise ctl.ControlError(503, "simulator_unavailable", "The simulator is still starting. Try again in a moment.")
        try:
            g, loc = layout_index(layout, bid)
        except KeyError:
            raise ctl.ControlError(404, "unknown_battery", f"There is no battery called {bid!r}.") from None
        pipe = self.r.pipeline(transaction=False)
        pipe.hmget(self.keys.battery(bid), ("soc", "owner", "available", "capacity_kwh", "power_kw"))
        pipe.getrange(self.keys.kw, g * 4, g * 4 + 3)
        (soc, owner, available, cap, power), kw_raw = pipe.execute()
        if cap is None:
            raise ctl.ControlError(503, "simulator_unavailable", "The simulator is still starting. Try again in a moment.")
        kw_now = float(np.frombuffer(kw_raw, dtype=np.float32)[0]) if kw_raw and len(kw_raw) == 4 else 0.0
        owner_i = int(owner) if owner is not None else None
        return {
            "battery_id": bid, "location": loc, "city": geo.city_name(loc),
            "lonlat": battery_lonlat(g, loc, int(layout.get("seed", self.cfg.seed))),
            "worker_id": owner_i if owner_i is None or owner_i >= 0 else None,
            "capacity_kwh": float(cap), "power_kw": float(power), "charge_level": round(float(soc), 6),
            "power_kw_now": round(kw_now, 4), "available": available in (b"1", b"True", b"true"),
        }


# --- chaos ---------------------------------------------------------------------------------------


def _proc_info(pid: int) -> tuple[str | None, bool]:
    """(command line, is_zombie) of a process; (None, False) if it doesn't exist."""
    proc = Path(f"/proc/{pid}")
    if proc.exists():
        try:
            cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            state = (proc / "stat").read_text().rsplit(")", 1)[-1].split()[0]
            return cmd or None, state == "Z"
        except (OSError, IndexError):
            return None, False
    try:
        out = subprocess.run(["ps", "-o", "stat=,command=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None, False
    line = out.stdout.strip()
    if not line:
        return None, False
    stat, _, cmd = line.partition(" ")
    return cmd.strip() or None, "Z" in stat


def kill_worker(backend: Backend, worker_id: int | None, rng=None) -> dict:
    """SIGKILL one worker process (never the coordinator, the API or Redis)."""
    import random

    snap = backend.snapshot()
    workers = {int(w["worker_id"]): w for w in snap.get("workers", [])}
    if not workers:
        raise ctl.ControlError(503, "simulator_unavailable", "The simulator's controllers aren't running yet.")
    # a worker killed moments ago still looks healthy until the coordinator notices (~2 ticks)
    killed_key = backend.keys.prefix + "chaos:killed"
    recently_killed = {int(p) for p in backend.r.zrangebyscore(killed_key, time.time() - 60, "+inf")}
    for info in workers.values():
        if info.get("pid") in recently_killed and info.get("status") != "dead":
            info["status"] = "dead"
    healthy = sorted(w for w, info in workers.items() if info.get("status") == "healthy")
    if worker_id is None:
        if len(healthy) - 1 < 2:
            raise ctl.ControlError(409, "too_few_healthy", "Crashing another controller would leave fewer than 2 working.")
        worker_id = (rng or random).choice(healthy)
    if worker_id not in workers:
        raise ctl.ControlError(404, "unknown_worker", f"There's no controller with worker_id {worker_id}; "
                               f"ids run from 0 to {max(workers)}.")
    if workers[worker_id].get("status") == "dead":
        raise ctl.ControlError(409, "already_dead", f"{ctl.controller(worker_id)} has already crashed.")
    if len([w for w in healthy if w != worker_id]) < 2:
        raise ctl.ControlError(409, "too_few_healthy", "Crashing this controller would leave fewer than 2 working.")
    entry = backend.registry().get(worker_id) or {}
    pid = entry.get("pid") or workers[worker_id].get("pid")
    protected = {os.getpid(), os.getppid(), backend.coordinator_pid(), backend.redis_pid(), 0, 1, None}
    if pid in protected:
        raise ctl.ControlError(409, "not_a_worker", "That process isn't a controller, so it was left alone.")
    cmd, zombie = _proc_info(int(pid))
    if cmd is None or zombie:
        raise ctl.ControlError(409, "already_dead", f"{ctl.controller(worker_id)} has already crashed.")
    if "src.sim.worker" not in cmd:
        raise ctl.ControlError(409, "not_a_worker", "That process isn't a controller, so it was left alone.")
    try:
        os.kill(int(pid), signal.SIGKILL)
    except ProcessLookupError:
        raise ctl.ControlError(409, "already_dead", f"{ctl.controller(worker_id)} has already crashed.") from None
    pipe = backend.r.pipeline(transaction=False)
    pipe.zadd(killed_key, {str(int(pid)): time.time()})
    pipe.zremrangebyscore(killed_key, "-inf", time.time() - 120)
    pipe.execute()
    return {"worker_id": worker_id, "pid": int(pid), "killed_at_utc": now_utc_iso()}
