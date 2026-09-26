"""WebSocket /live messages (docs/API.md): one `snapshot` on connect, then a `frame` every
second with EVERY tick since the previous frame (never sampled), or an `idle` frame when
no run is active. A new run (run_id changed) gets a fresh `snapshot`.
"""
from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.api.backend import Backend

UTC = timezone.utc
HISTORY_TICKS = 2000
SNAPSHOT_EVENTS = 30


@dataclass
class LiveCursor:
    """Where one connection is in the tick and event streams."""

    run_id: str | None = None
    ticks: str = "0-0"
    events: str = "0-0"
    forecast_issued: dict = field(default_factory=dict)


def snapshot_message(backend: Backend, cursor: LiveCursor | None = None, *, history: int = HISTORY_TICKS) -> dict:
    cursor = cursor or LiveCursor()
    snap = backend.snapshot()
    run_id = snap.get("run_id")
    ticks, cursor.ticks = backend.history(run_id, history) if run_id else ([], backend.history(None, 1)[1])
    events, cursor.events = backend.events_recent(SNAPSHOT_EVENTS)
    cursor.run_id = run_id
    forecast = forecasts.at(snap.get("sim_time_utc"), backend) if run_id else {}
    cursor.forecast_issued = {loc: f.get("issued_utc") for loc, f in forecast.items()}
    return {
        "type": "snapshot",
        "run_id": run_id,
        "status": snap.get("status", "idle"),
        "settings": snap.get("settings"),
        "history": ticks,
        "totals": snap.get("totals"),
        "workers": snap.get("workers", []),
        "failed_commands": backend.failed_commands(),
        "availability": snap.get("availability"),
        "events": events,
        "forecast": forecast,
    }


def frame_message(backend: Backend, cursor: LiveCursor) -> dict:
    snap = backend.snapshot()
    run_id = snap.get("run_id")
    if run_id != cursor.run_id and run_id is not None:
        return snapshot_message(backend, cursor)  # a new run: start the client over
    cursor.run_id = run_id
    ticks, cursor.ticks = backend.ticks_since(run_id, cursor.ticks) if run_id else ([], cursor.ticks)
    events, cursor.events = backend.events_since(cursor.events)
    status = snap.get("status", "idle")
    msg = {
        "type": "idle" if status == "idle" or run_id is None else "frame",
        "run_id": run_id,
        "status": status,
        "sim_time_utc": snap.get("sim_time_utc"),
        "speed_tps": snap.get("speed_tps"),
        "ticks": ticks,
        "totals": snap.get("totals"),
        "workers": snap.get("workers", []),
        "failed_commands": backend.failed_commands(),
        "availability": snap.get("availability"),
        "events": events,
    }
    if run_id:
        forecast = forecasts.at(snap.get("sim_time_utc"), backend)
        issued = {loc: f.get("issued_utc") for loc, f in forecast.items()}
        if forecast and issued != cursor.forecast_issued:
            cursor.forecast_issued = issued
            msg["forecast"] = forecast
    return msg


class ForecastCache:
    """Latest forecast per location at the replay's sim time, from src.forecast.provider (built in
    parallel; imported lazily). One lookup per location per 15-minute slot, shared by all viewers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slot: str | None = None
        self._value: dict = {}
        self._announced: set = set()

    def at(self, sim_time_utc: str | None, backend: Backend | None = None) -> dict:
        if not sim_time_utc:
            return {}
        try:
            t = datetime.fromisoformat(sim_time_utc.replace("Z", "+00:00"))
        except ValueError:
            return {}
        slot_t = t - timedelta(minutes=t.minute % 15, seconds=t.second)
        slot = slot_t.isoformat()
        with self._lock:
            if slot == self._slot:
                return self._value
        try:
            provider = importlib.import_module("src.forecast.provider")
        except ImportError:
            return {}
        out = {}
        for loc in (backend.cfg.fleet.locations if backend else ()):
            try:
                f = provider.latest_forecast(loc, t)
            except Exception:
                f = None
            if f:
                out[loc] = f
        with self._lock:
            self._slot, self._value = slot, out
        if backend is not None:
            for loc, f in out.items():
                issued = f.get("issued_utc")
                if issued and (loc, issued) not in self._announced:
                    self._announced.add((loc, issued))
                    from src.sim.control import area

                    try:
                        backend.emit("forecast_issued", "info", f"A new AI price forecast for the {area(loc)} is ready.",
                                     detail={"location": loc, "issued_utc": issued, "model": f.get("model")},
                                     once_key=f"forecast:{loc}:{issued}")
                    except Exception:
                        pass
        return out


forecasts = ForecastCache()
