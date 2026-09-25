"""In-memory BatteryAdapter: numpy state arrays plus the per-battery fence.

Used by the M0 skeleton and by the tests. The Redis-hash adapter (M2) must
behave the same way. Both run tests/test_contracts_battery.py.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable

import numpy as np

from src.core.models import BatteryState, Command
from src.sim import battery as physics


class InMemoryBatteryAdapter:
    """BatteryAdapter over arrays of batteries. Each apply() runs physics.step on one battery.

    Idempotency (RUNLOG): each battery keeps last_command_id and last_tick.
    A command with the same id, or with tick_ts <= last_tick, is a silent
    no-op. `counters` records applied, duplicate and stale commands for
    observability.
    """

    def __init__(
        self,
        states: Iterable[BatteryState],
        *,
        tick_seconds: int = 300,
        soc_min: float = physics.SOC_MIN,
        soc_max: float = physics.SOC_MAX,
    ) -> None:
        states = list(states)
        ids = [s.battery_id for s in states]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate battery_id")
        if tick_seconds <= 0 or not (0.0 <= soc_min < soc_max <= 1.0):
            raise ValueError("need tick_seconds > 0 and 0 <= soc_min < soc_max <= 1")
        self._ids = ids
        self._index = {bid: i for i, bid in enumerate(ids)}
        self._capacity = np.array([s.capacity_kwh for s in states], dtype=np.float64)
        self._power = np.array([s.power_kw for s in states], dtype=np.float64)
        self._soc = np.array([s.soc for s in states], dtype=np.float64)
        self._efficiency = np.array([s.efficiency for s in states], dtype=np.float64)
        self._available = np.array([s.available for s in states], dtype=bool)
        if states and not (
            np.all(self._capacity > 0)
            and np.all(self._power >= 0)
            and np.all((self._efficiency > 0) & (self._efficiency <= 1))
            and np.all(np.isfinite(self._soc))
        ):
            raise ValueError("need capacity_kwh > 0, power_kw >= 0, 0 < efficiency <= 1, finite soc")
        self._dt_h = tick_seconds / 3600.0
        self._soc_min = soc_min
        self._soc_max = soc_max
        self._last_command_id: list[str | None] = [None] * len(ids)
        self._last_tick: list[datetime | None] = [None] * len(ids)
        self.counters = {"applied": 0, "duplicate": 0, "stale": 0}

    # --- BatteryAdapter --------------------------------------------------------------

    def apply(self, cmd: Command) -> None:
        i = self._position(cmd.battery_id)
        if cmd.tick_ts.tzinfo is None or cmd.tick_ts.utcoffset() is None:
            raise ValueError(f"tick_ts must be timezone-aware, got {cmd.tick_ts!r}")
        if not math.isfinite(cmd.setpoint_kw):
            raise ValueError(f"setpoint_kw must be finite, got {cmd.setpoint_kw!r}")
        if cmd.command_id == self._last_command_id[i]:
            self.counters["duplicate"] += 1
            return
        last_tick = self._last_tick[i]
        if last_tick is not None and cmd.tick_ts <= last_tick:
            self.counters["stale"] += 1
            return
        one = slice(i, i + 1)
        new_soc, _ = physics.step(
            self._soc[one], self._capacity[one], self._power[one], self._efficiency[one],
            self._available[one], cmd.setpoint_kw, self._dt_h, self._soc_min, self._soc_max,
        )
        self._soc[i] = new_soc[0]
        self._last_command_id[i] = cmd.command_id
        self._last_tick[i] = cmd.tick_ts
        self.counters["applied"] += 1

    def read(self, battery_id: str) -> BatteryState:
        i = self._position(battery_id)
        return BatteryState(
            battery_id=battery_id,
            capacity_kwh=float(self._capacity[i]),
            power_kw=float(self._power[i]),
            soc=float(self._soc[i]),
            efficiency=float(self._efficiency[i]),
            available=bool(self._available[i]),
        )

    # --- sim-only extras (not part of the frozen seam) ----------------------------------

    @property
    def battery_ids(self) -> list[str]:
        return list(self._ids)

    def set_available(self, battery_id: str, available: bool) -> None:
        """Take a battery offline or bring it back (visitors toggle this later)."""
        self._available[self._position(battery_id)] = bool(available)

    def _position(self, battery_id: str) -> int:
        try:
            return self._index[battery_id]
        except KeyError:
            raise KeyError(f"unknown battery_id {battery_id!r}") from None
