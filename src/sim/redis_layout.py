"""Redis layout of the orchestrated fleet (spec section 7), shared by the coordinator,
the worker processes, the Redis BatteryAdapter and the API.

Keys (all under an optional prefix, "" in production; tests use a unique one):

  battery:{battery_id}   hash, one per battery. The source of truth any worker can
                         adopt a battery from. Fields, dynamic ones first so HSET
                         finds them early in the listpack:
                           soc, last_command_id, last_tick, owner_epoch, available,
                           owner, run_id, battery_id, location, idx,
                           capacity_kwh, power_kw, efficiency
                         Workers write soc + last_command_id + last_tick together
                         (the idempotency fence) every tick; the coordinator writes
                         owner/owner_epoch on (re)assignment and available on changes.
  commands:{worker_id}   stream: one msgpack batch per worker per tick (plus 'load'/'stop').
  telemetry              stream: per-tick cohort summaries from workers, including
                         per-battery SoC and delivered kW (msgpack).
  hb:{worker_id}         heartbeat: SET every 2 s with a 6 s TTL.
  lease:{worker_id}      the worker's epoch. The coordinator overwrites it when it
                         declares the worker DEAD; the worker's WATCHed write fails,
                         so a zombie can never commit again (owner_epoch fencing).
  dead_letter            stream of failed commands with a reason; dead_letter:count.
  control                stream: API -> coordinator requests; reply:{op_id} lists.
  snapshot               JSON: status, settings, workers, totals, availability.
  events                 stream: plain-language events (coordinator/API -> website).
  ticks                  stream: one JSON Tick per sim tick (docs/API.md).
  workers                hash: worker registry, worker_id -> {pid, epoch, ...}.
  layout                 JSON: fleet layout (size, workers, locations and counts).
  kw                     float32 bytes: last delivered kW per battery (global order).
  epoch                  INCR counter for worker epochs.

Every stream is trimmed with MAXLEN ~.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

import msgpack
import numpy as np

from src.core.ids import COMMAND_NAMESPACE

UTC = timezone.utc
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"

# Approximate MAXLEN per stream (MAXLEN ~ keeps at least this many entries).
DEFAULT_MAXLEN = {
    "commands": 64,
    "telemetry": 400,
    "dead_letter": 20000,
    "events": 500,
    "ticks": 2100,
    "control": 500,
}

# Battery hash fields. DYNAMIC first: listpack hashes are scanned linearly.
F_SOC = "soc"
F_CID = "last_command_id"
F_TICK = "last_tick"
F_EPOCH = "owner_epoch"
F_AVAIL = "available"
F_OWNER = "owner"
F_RUN = "run_id"
F_ID = "battery_id"
F_LOC = "location"
F_IDX = "idx"
F_CAP = "capacity_kwh"
F_POW = "power_kw"
F_EFF = "efficiency"
HASH_FIELDS = (F_SOC, F_CID, F_TICK, F_EPOCH, F_AVAIL, F_OWNER, F_RUN, F_ID, F_LOC, F_IDX, F_CAP, F_POW, F_EFF)
NO_TICK = -1  # last_tick before any command was applied


def redis_url() -> str:
    return os.environ.get("REDIS_URL") or DEFAULT_REDIS_URL


def redis_prefix() -> str:
    return os.environ.get("FLEET_REDIS_PREFIX", "")


def connect(url: str | None = None, **kwargs):
    """A redis-py client. socket_timeout stays above every XREAD/BLPOP block we issue (< 5 s)."""
    import redis

    kwargs.setdefault("socket_timeout", 10.0)
    kwargs.setdefault("socket_connect_timeout", 5.0)
    kwargs.setdefault("health_check_interval", 0)
    return redis.Redis.from_url(url or redis_url(), **kwargs)


def reachable(url: str | None = None, timeout: float = 0.5) -> bool:
    """True if a Redis server answers PING at url (used by tests to skip cleanly)."""
    try:
        client = connect(url, socket_timeout=timeout, socket_connect_timeout=timeout)
        ok = bool(client.ping())
        client.close()
        return ok
    except Exception:
        return False


class Keys:
    """Key names under one prefix."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        p = prefix
        self.telemetry = p + "telemetry"
        self.dead_letter = p + "dead_letter"
        self.dead_letter_count = p + "dead_letter:count"
        self.control = p + "control"
        self.snapshot = p + "snapshot"
        self.events = p + "events"
        self.ticks = p + "ticks"
        self.workers = p + "workers"
        self.layout = p + "layout"
        self.kw = p + "kw"
        self.epoch = p + "epoch"
        self.coordinator = p + "coordinator"

    def commands(self, worker_id: int) -> str:
        return f"{self.prefix}commands:{worker_id}"

    def hb(self, worker_id: int) -> str:
        return f"{self.prefix}hb:{worker_id}"

    def lease(self, worker_id: int) -> str:
        return f"{self.prefix}lease:{worker_id}"

    def battery(self, battery_id: str) -> str:
        return f"{self.prefix}battery:{battery_id}"

    def reply(self, op_id: str) -> str:
        return f"{self.prefix}reply:{op_id}"

    def pattern(self) -> str:
        return f"{self.prefix}*"


# --- fleet layout -------------------------------------------------------------------------


def location_counts(size: int, shares: Mapping[str, float]) -> list[tuple[str, int]]:
    """Split `size` batteries by location share, in config order; the last location takes the remainder."""
    names = list(shares)
    counts = []
    for name in names[:-1]:
        counts.append((name, int(round(size * shares[name]))))
    counts.append((names[-1], size - sum(n for _, n in counts)))
    if any(n < 0 for _, n in counts):
        raise ValueError(f"cannot split {size} batteries by {dict(shares)}")
    return counts


def battery_id(location: str, k: int) -> str:
    """Battery ids look like LZ_HOUSTON-000123 (docs/API.md); k is the index within the location."""
    return f"{location}-{k:06d}"


@dataclass(frozen=True, eq=False)
class FleetLayout:
    """Static fleet: global battery order, location split and seeded specs.

    Global index g in [0, size): locations are contiguous in config order, so
    LZ_HOUSTON-000000.. come first, then LZ_NORTH-000000... Battery g gets row g
    of src.sim.fleet.generate_batteries(size, ...), so specs are prefix-stable.
    """

    locations: tuple[str, ...]
    counts: tuple[int, ...]
    capacity_kwh: np.ndarray
    power_kw: np.ndarray
    efficiency: np.ndarray
    workers: int
    seed: int = 0
    loc_of: np.ndarray = field(default=None)  # type: ignore[assignment]  # int8 location index per battery
    ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.loc_of is None:
            object.__setattr__(self, "loc_of", np.repeat(np.arange(len(self.counts), dtype=np.int8), self.counts))
        if not self.ids:
            ids = tuple(battery_id(loc, k) for loc, n in zip(self.locations, self.counts) for k in range(n))
            object.__setattr__(self, "ids", ids)

    @classmethod
    def build(cls, cfg, size: int | None = None, workers: int | None = None) -> "FleetLayout":
        """From the typed config (src.config.Config); size/workers override fleet.size/fleet.workers."""
        from src.sim.fleet import generate_batteries

        size = cfg.fleet.size if size is None else int(size)
        workers = cfg.fleet.workers if workers is None else int(workers)
        if size < 1 or workers < 1:
            raise ValueError("need fleet size >= 1 and workers >= 1")
        split = location_counts(size, cfg.fleet.locations)
        states = generate_batteries(size, cfg.battery, cfg.seed)
        return cls(
            locations=tuple(n for n, _ in split),
            counts=tuple(c for _, c in split),
            capacity_kwh=np.array([s.capacity_kwh for s in states], dtype=np.float64),
            power_kw=np.array([s.power_kw for s in states], dtype=np.float64),
            efficiency=np.array([s.efficiency for s in states], dtype=np.float64),
            workers=workers,
            seed=cfg.seed,
        )

    @property
    def size(self) -> int:
        return int(sum(self.counts))

    @property
    def offsets(self) -> list[int]:
        return [int(x) for x in np.concatenate([[0], np.cumsum(self.counts)[:-1]])]

    def location_indices(self, location: str | int) -> np.ndarray:
        li = self.locations.index(location) if isinstance(location, str) else int(location)
        start = self.offsets[li]
        return np.arange(start, start + self.counts[li], dtype=np.int32)

    def index_of(self, bid: str) -> int:
        """Global index of a battery id. Raises KeyError for an unknown id."""
        loc, _, k = bid.rpartition("-")
        if loc not in self.locations or not k.isdigit() or len(k) != 6:
            raise KeyError(bid)
        li = self.locations.index(loc)
        k = int(k)
        if not 0 <= k < self.counts[li]:
            raise KeyError(bid)
        return self.offsets[li] + k

    def default_owners(self, workers: int | None = None) -> np.ndarray:
        """Initial cohorts: each location is split into `workers` contiguous chunks, so worker w
        runs chunk w of every location (1,000 = 500 Houston + 500 Dallas-Fort Worth at 20k/20)."""
        workers = self.workers if workers is None else workers
        owners = np.empty(self.size, dtype=np.int16)
        for li, n in enumerate(self.counts):
            start = self.offsets[li]
            for w, chunk in enumerate(np.array_split(np.arange(n), workers)):
                owners[start + chunk] = w
        return owners

    def nameplate_kw(self) -> float:
        return float(self.power_kw.sum())

    def public(self) -> dict:
        """JSON-able summary for the API (ids are derived from locations + counts)."""
        return {
            "size": self.size,
            "workers": self.workers,
            "locations": [[loc, int(n)] for loc, n in zip(self.locations, self.counts)],
            "nameplate_kw": self.nameplate_kw(),
            "capacity_kwh": float(self.capacity_kwh.sum()),
            "seed": self.seed,
        }


def layout_index(layout_json: Mapping, bid: str) -> tuple[int, str]:
    """(global index, location) of a battery id against a published layout. KeyError if unknown."""
    loc, _, k = bid.rpartition("-")
    offset = 0
    for name, n in layout_json["locations"]:
        if name == loc:
            if not (k.isdigit() and len(k) == 6 and 0 <= int(k) < int(n)):
                raise KeyError(bid)
            return offset + int(k), loc
        offset += int(n)
    raise KeyError(bid)


# --- command ids ---------------------------------------------------------------------------

_HEX = np.frombuffer(b"".join(f"{i:02x}".encode() for i in range(256)), dtype=np.uint8).reshape(256, 2)
_NS = COMMAND_NAMESPACE.bytes


class CommandIds:
    """src.core.ids.make_command_id for many batteries at once: identical ids, about 6x faster.

    uuid5 = sha1(namespace + f"{run_id}|{battery_id}|{tick UTC isoformat}") with the
    version and variant bits set. The per-battery prefix hash is computed once per run,
    and the flag-setting and hex formatting are vectorized.
    """

    def __init__(self, run_id) -> None:
        self.run_id = str(run_id)

    def prefixes(self, battery_ids: Iterable[str]) -> list:
        run = self.run_id
        return [hashlib.sha1(_NS + f"{run}|{bid}|".encode(), usedforsecurity=False) for bid in battery_ids]

    @staticmethod
    def generate(prefixes: Sequence, tick_ts: datetime | int) -> np.ndarray:
        """Command ids (numpy 'S36' ASCII) for one tick, one per prefix."""
        if isinstance(tick_ts, (int, np.integer)):
            tick_ts = datetime.fromtimestamp(int(tick_ts), UTC)
        if tick_ts.tzinfo is None or tick_ts.utcoffset() is None:
            raise ValueError(f"tick_ts must be timezone-aware, got {tick_ts!r}")
        if not prefixes:
            return np.empty(0, dtype="S36")
        suffix = tick_ts.astimezone(UTC).isoformat().encode()
        digests = []
        append = digests.append
        for p in prefixes:
            h = p.copy()
            h.update(suffix)
            append(h.digest())
        a = np.frombuffer(b"".join(digests), dtype=np.uint8).reshape(-1, 20)[:, :16].copy()
        a[:, 6] = (a[:, 6] & 0x0F) | 0x50
        a[:, 8] = (a[:, 8] & 0x3F) | 0x80
        hx = _HEX[a].reshape(-1, 32)
        out = np.empty((hx.shape[0], 36), dtype=np.uint8)
        out[:, 0:8] = hx[:, 0:8]
        out[:, 9:13] = hx[:, 8:12]
        out[:, 14:18] = hx[:, 12:16]
        out[:, 19:23] = hx[:, 16:20]
        out[:, 24:36] = hx[:, 20:32]
        out[:, [8, 13, 18, 23]] = ord("-")
        return out.reshape(-1).view("S36")

    def ids(self, battery_ids: Sequence[str], tick_ts: datetime | int) -> list[str]:
        return [x.decode() for x in self.generate(self.prefixes(battery_ids), tick_ts).tolist()]


# --- message codec -------------------------------------------------------------------------


def pack(obj) -> bytes:
    return msgpack.packb(obj, use_bin_type=True)


def unpack(data: bytes):
    return msgpack.unpackb(data, raw=False)


def i32(a) -> bytes:
    return np.ascontiguousarray(a, dtype=np.int32).tobytes()


def f64(a) -> bytes:
    return np.ascontiguousarray(a, dtype=np.float64).tobytes()


def u8(a) -> bytes:
    return np.ascontiguousarray(a, dtype=np.uint8).tobytes()


def arr(data: bytes, dtype) -> np.ndarray:
    return np.frombuffer(data, dtype=dtype)


def resp(*args) -> bytes:
    """One command in RESP2 wire format (the worker's hot write path builds its own)."""
    out = [b"*%d\r\n" % len(args)]
    for a in args:
        if isinstance(a, str):
            a = a.encode()
        elif not isinstance(a, (bytes, bytearray)):
            a = str(a).encode()
        out.append(b"$%d\r\n%s\r\n" % (len(a), a))
    return b"".join(out)


def to_utc_iso(ts: int | float | datetime | None) -> str | None:
    """'2025-05-16T21:05:00Z' (docs/API.md *_utc fields)."""
    if ts is None:
        return None
    if not isinstance(ts, datetime):
        ts = datetime.fromtimestamp(int(ts), UTC)
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def dead_letter_entry(*, command_id: str, battery_id: str, worker_id: int | None, tick_ts: int,
                      reason: str, run_id: str, setpoint_kw: float | None = None) -> dict:
    return {"j": json.dumps({
        "command_id": command_id, "battery_id": battery_id, "worker_id": worker_id,
        "tick_utc": to_utc_iso(tick_ts), "reason": reason, "run_id": run_id,
        "setpoint_kw": None if setpoint_kw is None else float(setpoint_kw), "wall_utc": now_utc_iso(),
    })}


def write_dead_letters(client, keys: Keys, entries: list[dict], maxlen: int = DEFAULT_MAXLEN["dead_letter"]) -> None:
    """XADD dead_letter entries (one per failed command) and bump the per-run counter."""
    if not entries:
        return
    pipe = client.pipeline(transaction=False)
    for e in entries:
        pipe.xadd(keys.dead_letter, e, maxlen=maxlen, approximate=True)
    pipe.incrby(keys.dead_letter_count, len(entries))
    pipe.execute()
