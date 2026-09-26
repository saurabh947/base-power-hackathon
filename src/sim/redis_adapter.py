"""BatteryAdapter over the per-battery Redis hashes (the frozen seam, spec section 4).

One command at a time, for anything that wants the plain Protocol (contract tests,
tools, a future hardware bridge). The worker processes use the same hash layout and the
same physics (src.sim.battery.step) but apply whole batches per tick.

apply(cmd): WATCH the battery hash, read soc + fence, dedupe on last_command_id AND
tick <= last_tick, step the physics, then write soc + last_command_id + last_tick in one
MULTI/EXEC. A concurrent writer makes EXEC fail and the command is re-evaluated, so a
command is applied at most once even with several adapters on one battery.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable

import numpy as np
import redis

from src.core.models import BatteryState, Command
from src.sim import battery as physics
from src.sim.redis_layout import (
    F_AVAIL, F_CAP, F_CID, F_EFF, F_EPOCH, F_ID, F_OWNER, F_POW, F_RUN, F_SOC, F_TICK, NO_TICK, Keys, connect,
)

UTC = timezone.utc


class RedisBatteryAdapter:
    def __init__(
        self,
        states: Iterable[BatteryState] | None = None,
        *,
        url: str | None = None,
        prefix: str = "",
        tick_seconds: int = 300,
        soc_min: float = physics.SOC_MIN,
        soc_max: float = physics.SOC_MAX,
        ttl_s: int | None = None,
        client=None,
    ) -> None:
        if tick_seconds <= 0 or not (0.0 <= soc_min < soc_max <= 1.0):
            raise ValueError("need tick_seconds > 0 and 0 <= soc_min < soc_max <= 1")
        self.r = client or connect(url)
        self.keys = Keys(prefix)
        self.dt_h = tick_seconds / 3600.0
        self.soc_min, self.soc_max = soc_min, soc_max
        self.ttl_s = ttl_s
        self.counters = {"applied": 0, "duplicate": 0, "stale": 0}
        if states is not None:
            self.seed(states)

    def seed(self, states: Iterable[BatteryState]) -> None:
        """Write fresh hashes (empty fence) for these batteries."""
        states = list(states)
        ids = [s.battery_id for s in states]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate battery_id")
        pipe = self.r.pipeline(transaction=False)
        for s in states:
            if not (s.capacity_kwh > 0 and s.power_kw >= 0 and 0 < s.efficiency <= 1 and math.isfinite(s.soc)):
                raise ValueError("need capacity_kwh > 0, power_kw >= 0, 0 < efficiency <= 1, finite soc")
            key = self.keys.battery(s.battery_id)
            pipe.delete(key)
            pipe.hset(key, mapping={
                F_SOC: repr(float(s.soc)), F_CID: "", F_TICK: NO_TICK, F_EPOCH: 0, F_AVAIL: int(bool(s.available)),
                F_OWNER: -1, F_RUN: "", F_ID: s.battery_id, F_CAP: repr(float(s.capacity_kwh)),
                F_POW: repr(float(s.power_kw)), F_EFF: repr(float(s.efficiency)),
            })
            if self.ttl_s:
                pipe.expire(key, self.ttl_s)
        pipe.execute()

    # --- BatteryAdapter ---------------------------------------------------------------------

    def apply(self, cmd: Command) -> None:
        if cmd.tick_ts.tzinfo is None or cmd.tick_ts.utcoffset() is None:
            raise ValueError(f"tick_ts must be timezone-aware, got {cmd.tick_ts!r}")
        if not math.isfinite(cmd.setpoint_kw):
            raise ValueError(f"setpoint_kw must be finite, got {cmd.setpoint_kw!r}")
        key = self.keys.battery(cmd.battery_id)
        tick = cmd.tick_ts.timestamp()
        while True:
            with self.r.pipeline(transaction=True) as pipe:
                try:
                    pipe.watch(key)
                    row = pipe.hmget(key, (F_SOC, F_CID, F_TICK, F_CAP, F_POW, F_EFF, F_AVAIL))
                    if row[3] is None:
                        pipe.unwatch()
                        raise KeyError(f"unknown battery_id {cmd.battery_id!r}")
                    soc, last_id, last_tick = float(row[0]), (row[1] or b"").decode(), float(row[2])
                    if cmd.command_id == last_id:
                        pipe.unwatch()
                        self.counters["duplicate"] += 1
                        return
                    if last_tick != NO_TICK and tick <= last_tick:
                        pipe.unwatch()
                        self.counters["stale"] += 1
                        return
                    available = row[6] in (b"1", b"True", b"true")
                    new_soc, _ = physics.step(
                        np.array([soc]), float(row[3]), float(row[4]), float(row[5]), available,
                        cmd.setpoint_kw, self.dt_h, self.soc_min, self.soc_max,
                    )
                    pipe.multi()
                    pipe.hset(key, mapping={F_SOC: repr(float(new_soc[0])), F_CID: cmd.command_id,
                                            F_TICK: _tick_repr(tick)})
                    pipe.execute()
                    self.counters["applied"] += 1
                    return
                except redis.WatchError:
                    continue  # someone else wrote this battery meanwhile: re-evaluate

    def read(self, battery_id: str) -> BatteryState:
        row = self.r.hmget(self.keys.battery(battery_id), (F_SOC, F_CAP, F_POW, F_EFF, F_AVAIL))
        if row[1] is None:
            raise KeyError(f"unknown battery_id {battery_id!r}")
        return BatteryState(battery_id=battery_id, capacity_kwh=float(row[1]), power_kw=float(row[2]),
                            soc=float(row[0]), efficiency=float(row[3]),
                            available=row[4] in (b"1", b"True", b"true"))

    # --- sim-only extras ------------------------------------------------------------------------

    def set_available(self, battery_id: str, available: bool) -> None:
        key = self.keys.battery(battery_id)
        if not self.r.exists(key):
            raise KeyError(f"unknown battery_id {battery_id!r}")
        self.r.hset(key, F_AVAIL, int(bool(available)))

    def fence(self, battery_id: str) -> tuple[str, datetime | None]:
        """(last_command_id, last_tick) as stored in the hash."""
        row = self.r.hmget(self.keys.battery(battery_id), (F_CID, F_TICK))
        if row[1] is None:
            raise KeyError(f"unknown battery_id {battery_id!r}")
        t = float(row[1])
        return (row[0] or b"").decode(), (None if t == NO_TICK else datetime.fromtimestamp(t, UTC))


def _tick_repr(tick: float) -> str:
    return str(int(tick)) if float(tick).is_integer() else repr(float(tick))
