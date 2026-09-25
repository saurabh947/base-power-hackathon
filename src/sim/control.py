"""Control plane shared by the API and the coordinator.

- Settings: defaults, presets and validation of visitor-editable settings (docs/API.md).
- Availability overrides (POST /availability): a per-battery offline flag, last write wins.
- Plain-language event messages (docs/UX.md tone: short, no jargon, units explained).
- ControlClient: the API's request/reply over Redis (control stream -> reply:{op_id} list).
"""
from __future__ import annotations

import json
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import numpy as np

from src.sim.redis_layout import DEFAULT_MAXLEN, Keys, now_utc_iso, to_utc_iso

UTC = timezone.utc
CHICAGO = ZoneInfo("America/Chicago")

STRATEGIES = ("bands", "forecast", "peak")
STRATEGY_LABELS = {
    "bands": ("Simple rule", "Buys when the price is in the cheapest quarter of the past week and sells in the top 10%."),
    "forecast": ("AI forecast (TimesFM-3)", "Uses an AI price forecast to decide when to buy and sell."),
    "peak": ("Evening peak", "Charges from 10 am to 3 pm and sells from 6 pm to 9 pm, every day."),
}
SPEEDS = (1, 2, 5, 10)
EDITABLE = ("preset", "speed_tps", "strategy", "start_charge_level")
AREA_NAMES = {"LZ_HOUSTON": "Houston area", "LZ_NORTH": "Dallas–Fort Worth area"}
EVENT_KINDS = (
    "run_started", "run_paused", "run_resumed", "speed_changed", "run_reset", "worker_suspect", "worker_dead",
    "worker_adopted", "availability_changed", "price_spike", "action_changed", "forecast_issued", "backtest_done",
)
SEVERITIES = ("info", "good", "warn")


class ControlError(Exception):
    """A request the coordinator or API refuses: HTTP status, error code, plain-language message."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


# --- wording ---------------------------------------------------------------------------------


def area(location: str) -> str:
    return AREA_NAMES.get(location, location)


def fmt_int(n: int | float) -> str:
    return f"{int(round(n)):,}"


def speed_phrase(tps) -> str:
    """'1 hour of market time every 6 seconds' for 2 ticks/s (5-minute ticks)."""
    if tps in (None, "max"):
        return "as fast as the computer can go"
    seconds = 12.0 / float(tps)
    text = f"{seconds:.1f}".rstrip("0").rstrip(".")
    return f"1 hour of market time every {text} second{'s' if seconds != 1 else ''}"


def price_phrase(usd_mwh: float) -> str:
    """'$2,250/MWh ($2.25 per kWh)' or '$120/MWh (12¢ per kWh)'."""
    per_kwh = usd_mwh / 1000.0
    small = f"{usd_mwh / 10:.0f}¢ per kWh" if abs(per_kwh) < 1 else f"${per_kwh:,.2f} per kWh"
    return f"${usd_mwh:,.0f}/MWh ({small})"


def local_clock(ts: int | datetime) -> str:
    """'May 16, 2025, 7:45 pm' in Houston time."""
    if not isinstance(ts, datetime):
        ts = datetime.fromtimestamp(int(ts), UTC)
    t = ts.astimezone(CHICAGO)
    hour = t.hour % 12 or 12
    return f"{t:%B} {t.day}, {t.year}, {hour}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def to_local_iso(ts: int | datetime) -> str:
    if not isinstance(ts, datetime):
        ts = datetime.fromtimestamp(int(ts), UTC)
    return ts.astimezone(CHICAGO).isoformat()


def make_event(kind: str, severity: str, message: str, *, sim_ts: int | None = None, detail: dict | None = None) -> dict:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")
    return {"wall_utc": now_utc_iso(), "sim_utc": to_utc_iso(sim_ts), "kind": kind, "severity": severity,
            "message": message, "detail": detail or {}}


def emit_event(client, keys: Keys, event: dict, maxlen: int = DEFAULT_MAXLEN["events"]) -> str:
    """XADD an event; its stream id becomes the event id readers see."""
    msg_id = client.xadd(keys.events, {"j": json.dumps(event)}, maxlen=maxlen, approximate=True)
    return msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)


ACTION_PHRASE = {"sell": "selling while prices are high", "charge": "charging while power is cheap", "hold": "waiting"}


def action_message(actions: Mapping[str, str]) -> str:
    """'Batteries in both areas are now selling while prices are high.'"""
    kinds = set(actions.values())
    if len(kinds) == 1:
        (only,) = kinds
        where = "both areas" if len(actions) == 2 else ("every area" if len(actions) > 2 else f"the {area(next(iter(actions)))}")
        return f"Batteries in {where} are now {ACTION_PHRASE[only]}."
    parts = [f"{area(loc)} batteries are now {ACTION_PHRASE[a]}" for loc, a in actions.items()]
    return "; ".join(parts) + "."


# --- settings ----------------------------------------------------------------------------------


def presets(raw: Mapping) -> list[dict]:
    items = (raw.get("presets") or {}).get("items") or []
    return [dict(p) for p in items]


def default_preset_id(raw: Mapping) -> str:
    return (raw.get("presets") or {}).get("default") or presets(raw)[0]["id"]


def api_section(raw: Mapping) -> dict:
    return dict(raw.get("api") or {})


def default_settings(cfg, raw: Mapping, fleet_size: int | None = None) -> dict:
    api = api_section(raw)
    preset_id = default_preset_id(raw)
    preset = next(p for p in presets(raw) if p["id"] == preset_id)
    return {
        "preset": preset_id,
        "start_utc": preset["start_utc"],
        "speed_tps": api.get("default_speed_tps", 2),
        "strategy": api.get("default_strategy", "forecast"),
        "start_charge_level": float(cfg.battery.demo_soc_init),
        "fleet_size": int(fleet_size if fleet_size is not None else cfg.fleet.size),
        "power_kw_range": list(cfg.battery.power_kw),
        "capacity_kwh_range": list(cfg.battery.capacity_kwh),
        "locations": dict(cfg.fleet.locations),
    }


def parse_speed(value, *, allow_max: bool = False):
    """1, 2, 5 or 10 ticks per second ('max' only for tests/benchmarks)."""
    if allow_max and value == "max":
        return "max"
    if isinstance(value, bool):
        raise ControlError(400, "invalid_speed", "Speed must be 1, 2, 5 or 10 steps per second.")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ControlError(400, "invalid_speed", "Speed must be 1, 2, 5 or 10 steps per second.") from None
    if not math.isfinite(f) or f not in SPEEDS:
        raise ControlError(400, "invalid_speed", "Speed must be 1, 2, 5 or 10 steps per second.")
    return int(f)


def resolve_settings(partial: Mapping | None, cfg, raw: Mapping, *, fleet_size: int | None = None,
                     allow_max: bool = False, allow_internal: bool = False) -> dict:
    """Defaults overlaid with the editable keys of `partial`; other keys are ignored.

    allow_internal also accepts start_utc (tests and the parity helper replay any day).
    """
    partial = dict(partial or {})
    settings = default_settings(cfg, raw, fleet_size)
    if "preset" in partial and partial["preset"] is not None:
        preset = next((p for p in presets(raw) if p["id"] == partial["preset"]), None)
        if preset is None:
            known = ", ".join(p["id"] for p in presets(raw))
            raise ControlError(400, "invalid_preset", f"There is no replay called {partial['preset']!r}. Try one of: {known}.")
        settings["preset"] = preset["id"]
        settings["start_utc"] = preset["start_utc"]
    if "speed_tps" in partial and partial["speed_tps"] is not None:
        settings["speed_tps"] = parse_speed(partial["speed_tps"], allow_max=allow_max)
    if "strategy" in partial and partial["strategy"] is not None:
        if partial["strategy"] not in STRATEGIES:
            raise ControlError(400, "invalid_strategy", "Decision method must be one of: bands, forecast, peak.")
        settings["strategy"] = partial["strategy"]
    if "start_charge_level" in partial and partial["start_charge_level"] is not None:
        value = partial["start_charge_level"]
        lo, hi = cfg.battery.soc_min, cfg.battery.soc_max
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (lo <= float(value) <= hi):
            raise ControlError(400, "invalid_start_charge_level",
                               f"Starting charge must be between {lo:.0%} and {hi:.0%} (a number from {lo} to {hi}).")
        settings["start_charge_level"] = float(value)
    if allow_internal and partial.get("start_utc"):
        try:
            parse_utc(partial["start_utc"])
        except ValueError:
            raise ControlError(400, "invalid_start", "start_utc must be an ISO-8601 time.") from None
        settings["start_utc"] = partial["start_utc"]
        settings["preset"] = partial.get("preset") or "custom"
    return settings


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("naive time")
    return dt.astimezone(UTC)


def choices(raw: Mapping, forecast_available: bool) -> dict:
    return {
        "strategies": [
            {"value": s, "label": STRATEGY_LABELS[s][0], "description": STRATEGY_LABELS[s][1],
             "available": (forecast_available if s == "forecast" else True)}
            for s in STRATEGIES
        ],
        "speeds": [{"value": s, "label": speed_phrase(s).capitalize()} for s in SPEEDS],
    }


# --- availability ------------------------------------------------------------------------------


class AvailabilityState:
    """Which batteries visitors took offline. Last write wins per battery.

    - scope=location: a seeded-random `offline_fraction` of that area is offline and the
      rest of the area is online. The same fraction always picks the same batteries
      (a fixed permutation per location), and a bigger fraction is a superset.
    - scope=worker: every battery the worker currently owns (the process keeps running).
    - scope=battery: one battery.
    - scope=all: every battery.
    """

    def __init__(self, layout, seed: int) -> None:
        self.layout = layout
        self.offline = np.zeros(layout.size, dtype=bool)
        self.offline_workers: set[int] = set()
        self._perm = [np.random.default_rng([int(seed), 1729, li]).permutation(n) for li, n in enumerate(layout.counts)]

    def clear(self) -> None:
        self.offline[:] = False
        self.offline_workers.clear()

    def apply(self, req: Mapping, owners: np.ndarray, n_workers: int) -> dict:
        """Apply one request. Returns {'affected': changed batteries, 'scope': ..., ...}."""
        before = self.offline.copy()
        scope = req.get("scope")
        detail: dict[str, Any] = {"scope": scope}
        if scope == "location":
            loc = req.get("location")
            if loc not in self.layout.locations:
                raise ControlError(400, "invalid_location", f"Unknown area {loc!r}. Use one of: {', '.join(self.layout.locations)}.")
            frac = req.get("offline_fraction")
            if isinstance(frac, bool) or not isinstance(frac, (int, float)) or not (0.0 <= float(frac) <= 1.0):
                raise ControlError(400, "invalid_fraction", "offline_fraction must be a number from 0 to 1.")
            li = self.layout.locations.index(loc)
            idx = self.layout.location_indices(li)
            n_off = int(round(float(frac) * len(idx)))
            area_off = np.zeros(len(idx), dtype=bool)
            area_off[self._perm[li][:n_off]] = True
            self.offline[idx] = area_off
            detail.update(location=loc, offline_fraction=float(frac), offline=n_off, total=len(idx))
        elif scope == "worker":
            w = req.get("worker_id")
            if isinstance(w, bool) or not isinstance(w, int) or not (0 <= w < n_workers):
                raise ControlError(404, "unknown_worker", f"There is no controller {w!r}. Controllers are numbered 0 to {n_workers - 1}.")
            available = _bool(req.get("available"))
            idx = np.flatnonzero(owners == w)
            self.offline[idx] = not available
            if available:
                self.offline_workers.discard(w)
            else:
                self.offline_workers.add(w)
            detail.update(worker_id=w, available=available, batteries=int(len(idx)))
        elif scope == "battery":
            bid = req.get("battery_id")
            try:
                g = self.layout.index_of(str(bid))
            except KeyError:
                raise ControlError(404, "unknown_battery", f"There is no battery called {bid!r}.") from None
            available = _bool(req.get("available"))
            self.offline[g] = not available
            detail.update(battery_id=bid, available=available)
        elif scope == "all":
            available = _bool(req.get("available", True))
            self.offline[:] = not available
            if available:
                self.offline_workers.clear()
            detail.update(available=available)
        else:
            raise ControlError(400, "invalid_scope", "scope must be one of: location, worker, battery, all.")
        # a worker stays listed only while all of its current batteries are still offline
        for w in list(self.offline_workers):
            mine = owners == w
            if mine.any() and not self.offline[mine].all():
                self.offline_workers.discard(w)
        changed = self.offline != before
        detail["affected"] = int(changed.sum())
        detail["changed_idx"] = np.flatnonzero(changed)
        return detail

    def summary(self) -> dict:
        by_location = {}
        for li, loc in enumerate(self.layout.locations):
            idx = self.layout.location_indices(li)
            by_location[loc] = {"available": int((~self.offline[idx]).sum()), "total": int(len(idx))}
        return {
            "available": int((~self.offline).sum()),
            "total": int(self.layout.size),
            "by_location": by_location,
            "offline_workers": sorted(self.offline_workers),
        }


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    raise ControlError(400, "invalid_available", "available must be true or false.")


def availability_message(detail: Mapping, summary: Mapping) -> str:
    online = f"{fmt_int(summary['available'])} of {fmt_int(summary['total'])} batteries are online."
    scope = detail.get("scope")
    if scope == "location":
        if detail["offline"] == 0:
            return f"Every battery in the {area(detail['location'])} is back online; {online}"
        return (f"{detail['offline_fraction']:.0%} of the {area(detail['location'])} batteries "
                f"({fmt_int(detail['offline'])}) are now offline; {online}")
    if scope == "worker":
        verb = "back online" if detail["available"] else "offline"
        return f"Controller {detail['worker_id']}'s {fmt_int(detail['batteries'])} batteries are {verb}; {online}"
    if scope == "battery":
        return f"Battery {detail['battery_id']} is {'back online' if detail['available'] else 'offline'}; {online}"
    return f"{'Every battery is back online' if detail.get('available', True) else 'Every battery is offline'}; {online}"


# --- API side of the control channel ---------------------------------------------------------------


class ControlClient:
    """Send one request to the coordinator and wait for its reply (API process, sync redis-py)."""

    def __init__(self, client, keys: Keys) -> None:
        self.r = client
        self.keys = keys

    def request(self, cmd: str, args: Mapping | None = None, timeout_s: float = 4.0) -> dict:
        op_id = uuid.uuid4().hex
        self.r.xadd(self.keys.control, {"j": json.dumps({"id": op_id, "cmd": cmd, "args": dict(args or {}),
                                                         "wall": time.time()})},
                    maxlen=DEFAULT_MAXLEN["control"], approximate=True)
        got = self.r.blpop([self.keys.reply(op_id)], timeout=max(0.01, float(timeout_s)))
        if got is None:
            raise ControlError(503, "simulator_unavailable",
                               "The simulator didn't answer in time. Please try again in a few seconds.")
        reply = json.loads(got[1])
        if not reply.get("ok"):
            extra = {k: v for k, v in reply.items() if k not in ("ok", "status", "error", "message")}
            raise ControlError(int(reply.get("status", 500)), reply.get("error", "internal"),
                               reply.get("message", "Something went wrong in the simulator."), **extra)
        return reply.get("result") or {}
