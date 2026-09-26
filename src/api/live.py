"""WebSocket /live messages (docs/API.md): one `snapshot` on connect, then a `frame` every
second with EVERY tick since the previous frame (never sampled) while a run exists, and an
`idle` message every 5 s when none does. A new run (run_id changed) gets a fresh `snapshot`.
The snapshot and every frame carry `battery_status` (the latest tick's packed codes, base64).
"""
from __future__ import annotations

import importlib
import threading
import time
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
        "sim_time_utc": snap.get("sim_time_utc"),
        "speed_tps": snap.get("speed_tps"),
        "settings": snap.get("settings") if run_id else None,  # no run, no replay settings (as the mock)
        "history": ticks,
        "totals": snap.get("totals"),
        "workers": snap.get("workers", []),
        "failed_commands": backend.failed_commands(),
        "availability": snap.get("availability"),
        "events": events,
        "forecast": forecast,
        "battery_status": backend.battery_status(_fleet_size(snap)),
    }


def _fleet_size(snap: dict) -> int | None:
    settings = snap.get("settings") or {}
    return settings.get("fleet_size")


def idle_message(snap: dict | None) -> dict:
    """docs/API.md: {"type": "idle", "status": "idle", "run_id": null, "last_run": {run_id, totals} | null}."""
    last = (snap or {}).get("last_run")
    return {"type": "idle", "status": "idle", "run_id": None,
            "last_run": {"run_id": last["run_id"], "totals": last["totals"]} if last else None}


def frame_message(backend: Backend, cursor: LiveCursor) -> dict:
    """The next message for one connection: a snapshot (new run), a frame (a run exists) or idle."""
    snap = backend.snapshot()
    run_id = snap.get("run_id")
    if run_id is None or snap.get("status") == "idle":
        cursor.run_id = None
        return idle_message(snap)
    if run_id != cursor.run_id:
        return snapshot_message(backend, cursor)  # a new run: start the client over
    ticks, cursor.ticks = backend.ticks_since(run_id, cursor.ticks)
    events, cursor.events = backend.events_since(cursor.events)
    status = snap.get("status", "idle")
    msg = {
        "type": "frame",
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
        "battery_status": backend.battery_status(_fleet_size(snap)),
    }
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
        self._retry_import_at = 0.0

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
            if time.monotonic() < self._retry_import_at:
                return {}
        try:
            provider = importlib.import_module("src.forecast.provider")
        except ImportError:  # not built yet: don't retry the import on every frame
            with self._lock:
                self._retry_import_at = time.monotonic() + 60.0
            return {}
        out = {}
        for loc in (backend.cfg.fleet.locations if backend else ()):
            try:
                f = provider.latest_forecast(loc, t)
            except Exception:
                f = None
            if f:
                out[loc] = _plain(f)
        with self._lock:
            self._slot, self._value = slot, out
        if backend is not None:
            fresh: dict[str, list[str]] = {}  # issued_utc -> cities: forecasts issued together share one event
            for loc, f in out.items():
                issued = f.get("issued_utc")
                if issued and (loc, issued) not in self._announced:
                    self._announced.add((loc, issued))
                    fresh.setdefault(issued, []).append(loc)
            for issued, locs in fresh.items():
                from src.sim import geo

                where = geo.cities_phrase(locs, total=len(backend.cfg.fleet.locations))
                msg = (f"A new AI price forecast for {where} is ready." if len(locs) == 1
                       else f"New AI price forecasts for {where} are ready.")
                try:
                    backend.emit("forecast_issued", "info", msg,
                                 detail={"location": locs[0], "locations": locs, "issued_utc": issued,
                                         "model": out[locs[0]].get("model")},
                                 once_key=f"forecast:{issued}:{','.join(sorted(locs))}")
                except Exception as exc:
                    print(f"[api] forecast_issued event not published: {exc!r}", flush=True)
        return out


def _plain(value):
    """JSON-ready copy of a provider result: datetimes become '...Z' strings, numpy becomes lists."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


forecasts = ForecastCache()
