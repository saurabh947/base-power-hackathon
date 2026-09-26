"""Coordinator: the fleet's tick loop, failure detection and recovery (spec section 7, M2 + M3).

Run:
    python -m src.sim.coordinator serve                 # what src.launcher starts
    python -m src.sim.coordinator bench --ticks 288     # M2: 1 sim day at max speed, prints ticks/s
    python -m src.sim.coordinator bench --ticks 60 --kill-at 20 [--speed 2]   # M3: SIGKILL a worker mid-run

One tick (5 sim-minutes):
  1. Per location, the Strategy sees decision_tick(now, published) with published =
     decision_lag_ticks back, price_history = earlier ticks only, the symmetric
     headroom H (sum of power_kw over commandable batteries) and their SoC.
  2. Its target is clipped to the feasible directional headroom and split per battery
     in proportion to feasible headroom (src.sim.allocation). Batteries that are
     offline (availability overrides) or owned by a SUSPECT/DEAD worker get 0.
  3. One msgpack batch per worker is XADDed to commands:{worker_id}.
  4. Deadline barrier on telemetry: wait until every worker reported or
     min(250 ms, 50% of the tick period) passed (250 ms at max speed). Never forever.
  5. Actual power = sum of reported delivered kW (a missing cohort counts as 0); the
     ledger records grid-side kWh per (tick, location) at that tick's RT price.
  6. A worker that missed the deadline is SUSPECT (its batteries get 0 next tick and the
     target is re-split over the survivors). It is DEAD after 2 consecutive misses or
     when its heartbeat key expires, whichever comes first. On DEAD the coordinator
     overwrites lease:{worker_id} (fencing the zombie), dead-letters the commands it
     never confirmed ('expired_owner_dead') and hands its cohort round-robin to the
     healthy workers, who HMGET it lazily with the very next tick's batch.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence

import numpy as np

from src.config import REPO_ROOT, Config, load_config, load_raw_config
from src.core.models import PriceTick
from src.ledger.backtest import BacktestLedger
from src.sim import control as ctl
from src.sim.allocation import allocate, fleet_headroom_kw
from src.sim.battery import fleet_soc, grid_kwh_from_soc_change, headroom_kw
from src.sim.decision import decision_tick
from src.sim.redis_layout import (
    DEFAULT_MAXLEN, F_AVAIL, F_CAP, F_CID, F_EFF, F_EPOCH, F_ID, F_IDX, F_LOC, F_OWNER, F_POW, F_RUN, F_SOC,
    F_TICK, NO_TICK, CommandIds, FleetLayout, Keys, connect, dead_letter_entry, f64, i32, now_utc_iso, pack,
    redis_prefix, redis_url, to_utc_iso, u8, unpack, write_dead_letters,
)

UTC = timezone.utc
HEALTHY, SUSPECT, DEAD, STARTING = "healthy", "suspect", "dead", "starting"


def _log(msg: str) -> None:
    print(f"[coordinator {os.getpid()}] {msg}", file=sys.stderr, flush=True)


@dataclass
class Orchestration:
    """Tunables (config.yaml `orchestration:`; RUNLOG decision 3)."""

    hb_interval_s: float = 2.0
    hb_ttl_s: float = 6.0
    miss_threshold: int = 2
    deadline_cap_ms: float = 250.0
    deadline_fraction: float = 0.5
    deadline_floor_ms: float = 100.0
    max_speed_deadline_ms: float = 250.0
    block_ms: int = 1000
    load_timeout_s: float = 20.0
    spawn_timeout_s: float = 30.0
    supervise_interval_s: float = 0.2
    snapshot_interval_s: float = 0.25
    action_event_min_ticks: int = 12
    price_spike_usd_mwh: float = 1000.0
    control_stale_s: float = 5.0
    maxlen: dict = field(default_factory=lambda: dict(DEFAULT_MAXLEN))

    @classmethod
    def from_config(cls, raw: Mapping, **overrides) -> "Orchestration":
        o = dict(raw.get("orchestration") or {})
        api = dict(raw.get("api") or {})
        kw = dict(
            hb_interval_s=float(o.get("heartbeat_interval_s", 2.0)),
            hb_ttl_s=float(o.get("heartbeat_ttl_s", 6.0)),
            miss_threshold=int(o.get("deadline_miss_threshold", 2)),
            deadline_cap_ms=float(o.get("deadline_cap_ms", 250.0)),
            deadline_fraction=float(o.get("deadline_fraction", 0.5)),
            deadline_floor_ms=float(o.get("deadline_floor_ms", 100.0)),
            max_speed_deadline_ms=float(o.get("max_speed_deadline_ms", 250.0)),
            block_ms=int(o.get("xread_block_ms", 1000)),
            load_timeout_s=float(o.get("load_timeout_s", 20.0)),
            spawn_timeout_s=float(o.get("spawn_timeout_s", 30.0)),
            action_event_min_ticks=int(api.get("action_event_min_ticks", 12)),
            price_spike_usd_mwh=float(api.get("price_spike_usd_mwh", 1000.0)),
            control_stale_s=float(api.get("control_timeout_s", 4.0)) + 1.0,
            maxlen={**DEFAULT_MAXLEN, **(o.get("stream_maxlen") or {})},
        )
        kw.update(overrides)
        if kw["block_ms"] >= 5000:
            raise ValueError("xread_block_ms must stay under 5 s (redis-py socket timeouts)")
        return cls(**kw)


# --- prices --------------------------------------------------------------------------------------

_FEEDS: dict = {}
_FEEDS_LOCK = threading.Lock()


def price_feed(years: tuple[int, ...]):
    """The PriceFeed for a run. env PRICE_SOURCE=snapshot (default, data/snapshot) | postgres."""
    source = os.environ.get("PRICE_SOURCE", "snapshot")
    key = (source,) if source == "postgres" else (source, years)
    with _FEEDS_LOCK:
        return _price_feed_locked(source, key, years)


def _price_feed_locked(source: str, key: tuple, years: tuple[int, ...]):
    if key not in _FEEDS:
        if source == "postgres":
            from src.feeds.replay import PostgresReplayFeed  # M1: the prices table (Cloud SQL in the container)

            _FEEDS[key] = PostgresReplayFeed(market="RT")
        elif source == "snapshot":
            from src.feeds.parquet import snapshot_feed

            _FEEDS[key] = snapshot_feed(years=years)
        else:
            raise ValueError(f"PRICE_SOURCE must be snapshot or postgres, got {source!r}")
    return _FEEDS[key]


def snapshot_years(start: datetime) -> tuple[int, ...]:
    from src.feeds.parquet import SNAPSHOT_DIR

    wanted = [start.year]
    if (start - timedelta(days=8)).year < start.year:
        wanted.insert(0, start.year - 1)
    years = tuple(y for y in wanted if (SNAPSHOT_DIR / f"ercot_rt_{y}_hubs.parquet").exists())
    return years or (start.year,)


class PriceWindow:
    """Real prices for one run, per location, pulled from a PriceFeed a day at a time.

    The warm-up starts exactly `warmup_s` before the replay start (7 days, as the M0 skeleton
    and the backtest load it), so the strategy sees exactly what they hand it:
      - now       = the location's tick at the delivery time,
      - published = the tick `lag` positions earlier in that location's tick list,
      - history   = the (at most `window`) prices before `published`, oldest first.
    In the first ticks of a run the history is therefore clamped by the warm-up start, as there.
    """

    def __init__(self, feed, locations: Sequence[str], start_ts: int, *, lag: int, window: int,
                 warmup_s: int, tick_s: int = 300, chunk_s: int = 86400) -> None:
        self.feed = feed
        self.locations = list(locations)
        self.lag, self.window, self.tick_s, self.chunk_s = lag, window, tick_s, chunk_s
        self._ts = {loc: np.empty(0, dtype=np.int64) for loc in self.locations}
        self._px = {loc: np.empty(0, dtype=np.float64) for loc in self.locations}
        self._ticks: dict[str, list[PriceTick]] = {loc: [] for loc in self.locations}
        self._start = start_ts
        self._loaded_until = start_ts - int(warmup_s)
        self.exhausted = False
        self.last_ts: int | None = None
        self.ensure(start_ts)

    GIVE_UP_S = 3 * 86400  # no price for 3 days past the last one: the data has ended

    def ensure(self, ts: int) -> None:
        while self._loaded_until <= ts + self.tick_s and not self.exhausted:
            a = self._loaded_until
            b = max(a + self.chunk_s, ts + self.chunk_s)  # the first call spans the whole warm-up
            got = False
            for loc in self.locations:
                ticks = list(self.feed.ticks(datetime.fromtimestamp(a, UTC), datetime.fromtimestamp(b, UTC), loc))
                if ticks:
                    got = True
                    stamps = np.array([int(t.ts.timestamp()) for t in ticks], dtype=np.int64)
                    self._ts[loc] = np.concatenate([self._ts[loc], stamps])
                    self._px[loc] = np.concatenate([self._px[loc], [t.price_usd_mwh for t in ticks]])
                    self._ticks[loc].extend(ticks)
                    self.last_ts = max(self.last_ts or 0, int(stamps[-1]))
            self._loaded_until = b
            since = self.last_ts if self.last_ts is not None else self._start
            if not got and b - since >= self.GIVE_UP_S:
                self.exhausted = True
        keep = self.window + self.lag + 8  # by position, so a gap can never cut into a history
        for loc in self.locations:
            cut = int(np.searchsorted(self._ts[loc], ts)) - keep
            if cut > 2 * self.window:  # trim rarely, in big steps
                self._ts[loc] = self._ts[loc][cut:]
                self._px[loc] = self._px[loc][cut:]
                del self._ticks[loc][:cut]

    def _index(self, loc: str, ts: int) -> int | None:
        stamps = self._ts[loc]
        i = int(np.searchsorted(stamps, ts))
        return i if i < len(stamps) and stamps[i] == ts else None

    def at(self, loc: str, ts: int) -> PriceTick | None:
        i = self._index(loc, ts)
        return None if i is None else self._ticks[loc][i]

    def decision_inputs(self, loc: str, ts: int, lag: int) -> tuple[PriceTick | None, PriceTick | None, list[float]]:
        """(now, published, history) for the tick delivered at ts."""
        i = self._index(loc, ts)
        if i is None:
            return None, None, []
        seen = i - lag
        if seen < 0:
            return self._ticks[loc][i], None, []  # nothing published yet
        return self._ticks[loc][i], self._ticks[loc][seen], self._px[loc][max(0, seen - self.window):seen].tolist()

    def ended(self, ts: int) -> bool:
        self.ensure(ts)
        if self.last_ts is None or ts > self.last_ts:  # past the last price: look ahead now, not a day at a time
            self.ensure(ts + 3 * self.chunk_s)
        return self.exhausted and (self.last_ts is None or ts > self.last_ts)


# --- strategies ----------------------------------------------------------------------------------

_STRATEGY_CACHE: dict = {}
STRATEGY_CACHE_S = 600.0  # forecast strategies load a year of forecasts; they are pure, so reuse them


def build_strategies(name: str, cfg: Config, locations: Sequence[str], *, start_ts: int | None = None,
                     tick_s: int = 300, refresh: bool = False) -> tuple[dict, str, str | None]:
    """One Strategy instance per location, from make_strategy(cfg.strategy, tick_seconds, name) with no
    extra context (as the backtest builds them). 'forecast' falls back to bands, with the reason, when it
    can't be built or its cached forecasts don't cover the first day of the replay."""
    from src.strategy import make_strategy

    warning = None
    if name == "forecast":
        try:
            cached = None if refresh else _STRATEGY_CACHE.get(name)
            if cached and cached[0] > time.monotonic() and set(cached[1]) >= set(locations):
                strategies = {loc: cached[1][loc] for loc in locations}
            else:
                strategies = {loc: make_strategy(cfg.strategy, cfg.sim.tick_seconds, name) for loc in locations}
                if all(len((getattr(s, "books", None) or {}).get(loc, ())) for loc, s in strategies.items()):
                    # only instances that hold forecasts: a later forecast job's output must not be ignored
                    _STRATEGY_CACHE[name] = (time.monotonic() + STRATEGY_CACHE_S, dict(strategies))
            if start_ts is not None:
                for loc, strategy in strategies.items():
                    cover = forecast_coverage(strategy, loc, start_ts, tick_s)
                    if cover is not None and cover < 0.9:
                        raise LookupError(f"forecasts cover {cover:.0%} of the first day for {loc}")
            return strategies, name, None
        except Exception as exc:  # no forecasts cached for these dates, or the module failed to load
            warning = f"{type(exc).__name__}: {exc}"
            name = "bands"
    return {loc: make_strategy(cfg.strategy, cfg.sim.tick_seconds, name) for loc in locations}, name, warning


def forecast_coverage(strategy, location: str, start_ts: int, tick_s: int, ticks: int = 288) -> float | None:
    """Share of the next `ticks` ticks with a usable forecast (issued at or before, not too old), as the
    backtest measures it. None for strategies that don't read forecasts."""
    books = getattr(strategy, "books", None)
    if books is None:
        return None
    book = books.get(location)
    if book is None or len(book) == 0:
        return 0.0
    ts_ns = (start_ts + np.arange(ticks, dtype=np.int64) * tick_s) * 10**9
    idx = np.searchsorted(book.issued, ts_ns, side="right") - 1
    age = ts_ns - book.issued[np.clip(idx, 0, None)]
    return float(np.mean((idx >= 0) & (age <= getattr(strategy, "max_age_ns", 6 * 3600 * 10**9))))


@dataclass
class Decision:
    raw_kw: float
    now: PriceTick | None
    published: PriceTick | None


class StrategyTargets:
    """Per-location Strategy with the decision lag (RUNLOG decision 5: no peeking)."""

    def __init__(self, strategies: dict, prices: PriceWindow, lag: int, tick_s: int) -> None:
        self.strategies, self.prices, self.lag, self.tick_s = strategies, prices, lag, tick_s

    def __call__(self, k: int, ts: int, loc: str, fsoc: float, headroom: float) -> Decision:
        now, published, history = self.prices.decision_inputs(loc, ts, self.lag)
        if now is None or published is None:
            return Decision(0.0, now, published)
        raw = self.strategies[loc].target_kw(decision_tick(now, published), fsoc, headroom, history)
        return Decision(float(raw), now, published)


class FixedTargets:
    """Given per-location raw targets per tick (parity runs). Prices only feed the ledger."""

    def __init__(self, targets: Mapping[str, Sequence[float]], prices: PriceWindow | None) -> None:
        self.targets = {loc: [float(x) for x in seq] for loc, seq in targets.items()}
        self.prices = prices
        self.length = max((len(v) for v in self.targets.values()), default=0)

    def __call__(self, k: int, ts: int, loc: str, fsoc: float, headroom: float) -> Decision:
        seq = self.targets.get(loc, [])
        raw = seq[k] if k < len(seq) else 0.0
        now = self.prices.at(loc, ts) if self.prices else None
        return Decision(raw, now, now)


# --- state ---------------------------------------------------------------------------------------


@dataclass
class Inflight:
    ts: int
    idx: np.ndarray
    sp: np.ndarray


@dataclass
class WorkerHandle:
    worker_id: int
    epoch: int
    proc: subprocess.Popen | None = None
    pid: int | None = None
    status: str = STARTING
    misses: int = 0
    owned: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int32))
    pending_load: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int32))
    pending_init: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=bool))
    inflight: dict = field(default_factory=dict)
    last_tick_ts: int | None = None
    adopted_from: list = field(default_factory=list)
    spawned_at: float = 0.0
    dead_reason: str | None = None
    exit_code: int | None = None
    ready_run: str | None = None

    def public(self) -> dict:
        return {"worker_id": self.worker_id, "status": self.status, "pid": self.pid,
                "batteries": int(len(self.owned)), "last_tick_utc": to_utc_iso(self.last_tick_ts),
                "adopted_from": list(self.adopted_from)}


@dataclass
class RunState:
    run_id: str
    settings: dict
    start_ts: int
    speed: float | None  # ticks per second; None = max
    status: str = "starting"
    k: int = 0
    decide: Callable | None = None
    prices: PriceWindow | None = None
    strategy_name: str = ""
    strategy_warning: str | None = None
    ledger: BacktestLedger | None = None
    revenue_usd: float = 0.0
    sold_kwh: float = 0.0
    bought_kwh: float = 0.0
    ticks_done: int = 0
    next_tick_at: float = 0.0
    initialized: np.ndarray | None = None
    max_ticks: int | None = None
    last_ts: int | None = None
    announced_action: dict = field(default_factory=dict)
    last_action_event_k: int = -10**9
    spike_active: dict = field(default_factory=dict)
    fixed: bool = False
    fixed_targets: Mapping | None = None


class Coordinator:
    def __init__(
        self,
        *,
        url: str | None = None,
        prefix: str | None = None,
        fleet_size: int | None = None,
        workers: int | None = None,
        cfg: Config | None = None,
        raw: Mapping | None = None,
        orchestration: Orchestration | None = None,
        parent_pid: int | None = None,
        journal: bool = False,
        allow_max_speed: bool = True,
    ) -> None:
        self.cfg = cfg or load_config()
        self.raw = dict(raw) if raw is not None else load_raw_config()
        self.url = url or redis_url()
        self.keys = Keys(redis_prefix() if prefix is None else prefix)
        self.orch = orchestration or Orchestration.from_config(self.raw)
        self.layout = FleetLayout.build(self.cfg, fleet_size, workers)
        self.r = connect(self.url)
        self.N = self.layout.size
        self.n_workers = self.layout.workers
        self.tick_s = self.cfg.sim.tick_seconds
        self.dt_h = self.cfg.sim.dt_hours
        self.lag = self.cfg.sim.decision_lag_ticks
        self.window = self.cfg.strategy.bands.window_days * self.cfg.sim.ticks_per_day
        self.warmup_s = self.cfg.strategy.bands.window_days * 86400  # the skeleton's and the backtest's warm-up
        self.soc_min, self.soc_max = self.cfg.battery.soc_min, self.cfg.battery.soc_max
        self.cap, self.power, self.eff = self.layout.capacity_kwh, self.layout.power_kw, self.layout.efficiency
        self.loc_idx = [self.layout.location_indices(li) for li in range(len(self.layout.locations))]
        self._bkey = [self.keys.battery(bid).encode() for bid in self.layout.ids]
        self.nameplate_kw = self.layout.nameplate_kw()
        self.soc = np.full(self.N, self.cfg.battery.soc_init)
        self.soc_k = np.full(self.N, -1, dtype=np.int64)
        self.kw = np.zeros(self.N)
        self.owner = self.layout.default_owners()
        self.availability = ctl.AvailabilityState(self.layout, self.cfg.seed)
        self.workers: list[WorkerHandle] = []
        self.run: RunState | None = None
        self.last_run: dict | None = None  # {"run_id", "totals"} of the run a reset or a new start replaced
        self.status = "idle"
        self.parent_pid = parent_pid
        self.journal_on = journal
        self.journal: dict[int, list] = {}  # tick -> [(idx, delivered kW)] from every report, on time or late
        self.allow_max_speed = allow_max_speed
        self.tick_hooks: list[Callable[[dict], None]] = []
        self.events: list[dict] = []
        self.tick_log: list[dict] = []
        self._stop = False
        self._tele_last = "0-0"
        self._control_last = "0-0"
        self._next_supervise = 0.0
        self._last_snapshot = 0.0
        self._snapshot_dirty = True
        self._booted = False

    # --- lifecycle --------------------------------------------------------------------------

    def boot(self, *, wait_healthy: bool = True) -> None:
        """Fresh namespace: layout, battery hashes, 20 worker processes."""
        self._clear_runtime_keys()
        self.r.set(self.keys.layout, json.dumps(self.layout.public()))
        self.r.set(self.keys.coordinator, json.dumps({"pid": os.getpid(), "started_utc": now_utc_iso()}))
        self._write_all_hashes(self.cfg.battery.soc_init)
        self._spawn_all()
        self._booted = True
        if wait_healthy:
            self.wait_workers_up(self.orch.spawn_timeout_s)
        self._publish_snapshot(force=True)

    def request_stop(self) -> None:
        self._stop = True

    def serve_forever(self) -> None:
        if not self._booted:
            self.boot(wait_healthy=False)
        try:
            while not self._stop:
                self.loop_once()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stop_workers()
        self.status = "idle"
        try:
            self._publish_snapshot(force=True)
        except Exception:
            pass

    def cleanup_keys(self) -> None:
        """Delete every key under our prefix (tests; refuses the empty production prefix)."""
        if not self.keys.prefix:
            raise RuntimeError("refusing to wipe the unprefixed namespace")
        batch = []
        for key in self.r.scan_iter(match=self.keys.pattern(), count=5000):
            batch.append(key)
            if len(batch) >= 5000:
                self.r.unlink(*batch)
                batch = []
        if batch:
            self.r.unlink(*batch)

    def _clear_runtime_keys(self) -> None:
        k = self.keys
        names = [k.telemetry, k.control, k.ticks, k.dead_letter, k.dead_letter_count, k.events, k.snapshot,
                 k.workers, k.kw]
        for w in range(self.n_workers):
            names += [k.commands(w), k.hb(w), k.lease(w)]
        self.r.delete(*names)
        self._tele_last = "0-0"
        self._control_last = "0-0"

    def _write_all_hashes(self, soc: float) -> None:
        ids, layout = self.layout.ids, self.layout
        epochs = {h.worker_id: h.epoch for h in self.workers}
        pipe = self.r.pipeline(transaction=False)
        for g in range(self.N):
            w = int(self.owner[g])
            pipe.hset(self.keys.battery(ids[g]), mapping={
                F_SOC: repr(float(soc)), F_CID: "", F_TICK: NO_TICK, F_EPOCH: epochs.get(w, 0),
                F_AVAIL: int(not self.availability.offline[g]), F_OWNER: w, F_RUN: "",
                F_ID: ids[g], F_LOC: layout.locations[int(layout.loc_of[g])], F_IDX: g,
                F_CAP: repr(float(self.cap[g])), F_POW: repr(float(self.power[g])), F_EFF: repr(float(self.eff[g])),
            })
            if len(pipe) >= 2000:
                pipe.execute()
        pipe.execute()
        self.r.set(self.keys.kw, np.zeros(self.N, dtype=np.float32).tobytes())

    def _spawn_all(self) -> None:
        epochs = [int(self.r.incr(self.keys.epoch)) for _ in range(self.n_workers)]
        self.workers = []
        for w, epoch in enumerate(epochs):
            self.workers.append(WorkerHandle(worker_id=w, epoch=epoch, owned=np.flatnonzero(self.owner == w).astype(np.int32)))
        # owner_epoch must name the new epochs before any worker can load a battery
        pipe = self.r.pipeline(transaction=False)
        for h in self.workers:
            pipe.set(self.keys.lease(h.worker_id), h.epoch)
            for g in h.owned.tolist():
                pipe.hset(self.keys.battery(self.layout.ids[g]), F_EPOCH, h.epoch)
            pipe.delete(self.keys.commands(h.worker_id), self.keys.hb(h.worker_id))
        pipe.execute()
        for h in self.workers:
            self._spawn_process(h)
        self._snapshot_dirty = True

    def _spawn_process(self, h: WorkerHandle) -> None:
        cmd = [
            sys.executable, "-m", "src.sim.worker", "--worker-id", str(h.worker_id), "--epoch", str(h.epoch),
            "--redis-url", self.url, "--prefix", self.keys.prefix, "--parent-pid", str(os.getpid()),
            "--hb-interval", str(self.orch.hb_interval_s), "--hb-ttl", str(self.orch.hb_ttl_s),
            "--block-ms", str(self.orch.block_ms),
        ]
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        h.proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL)
        h.pid = h.proc.pid
        h.status = STARTING
        h.spawned_at = time.monotonic()
        self.r.hset(self.keys.workers, str(h.worker_id), json.dumps(
            {"pid": h.pid, "epoch": h.epoch, "started_utc": now_utc_iso(), "coordinator_pid": os.getpid()}))

    def _stop_workers(self, timeout_s: float = 2.0) -> None:
        live = [h for h in self.workers if h.proc is not None and h.proc.poll() is None]
        try:
            pipe = self.r.pipeline(transaction=False)
            for h in live:
                pipe.xadd(self.keys.commands(h.worker_id), {"m": pack({"t": "stop"})},
                          maxlen=self.orch.maxlen["commands"], approximate=True)
            pipe.execute()
        except Exception:
            pass
        for h in live:
            try:
                h.proc.terminate()
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout_s
        for h in live:
            try:
                h.proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                h.proc.kill()
                h.proc.wait(timeout=5)
        for h in self.workers:
            if h.proc is not None:
                h.exit_code = h.proc.poll()

    def wait_workers_up(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._supervise(force=True)
            if all(h.status != STARTING for h in self.workers):
                return True
            time.sleep(0.05)
        return False

    # --- supervision ------------------------------------------------------------------------

    def _supervise(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._next_supervise:
            return
        self._next_supervise = now + self.orch.supervise_interval_s
        if self.parent_pid is not None and os.getppid() != self.parent_pid:
            _log(f"parent {self.parent_pid} is gone; shutting down")
            self._stop = True
        for h in self.workers:  # reap exited children (SIGKILLed workers stay zombies otherwise)
            if h.proc is not None and h.exit_code is None:
                rc = h.proc.poll()
                if rc is not None:
                    h.exit_code = rc
                    _log(f"worker {h.worker_id} (pid {h.pid}) exited with {rc}")
        if not self.workers:
            return
        beats = self.r.mget([self.keys.hb(h.worker_id) for h in self.workers])
        k_now = self.run.k if self.run else None
        for h, beat in zip(self.workers, beats):
            if h.status == DEAD:
                continue
            alive = False
            if beat is not None:
                try:
                    alive = int(json.loads(beat).get("epoch", -1)) == h.epoch
                except (ValueError, TypeError):
                    alive = False
            if h.status == STARTING:
                if alive:
                    h.status = HEALTHY
                    self._snapshot_dirty = True
                elif now - h.spawned_at > self.orch.spawn_timeout_s:
                    self._declare_dead(h, "no_heartbeat_at_start", k_now)
            elif not alive:
                self._declare_dead(h, "heartbeat_expired", k_now)
        if (self.owner < 0).any():
            self._reassign_orphans()

    # --- control ----------------------------------------------------------------------------

    def poll_control(self, block_ms: int) -> None:
        try:
            resp = self.r.xread({self.keys.control: self._control_last}, block=block_ms if block_ms > 0 else None,
                                count=32)
        except Exception as exc:
            _log(f"control read failed: {exc!r}")
            time.sleep(0.1)
            return
        for _name, entries in resp or []:
            for msg_id, fields in entries:
                self._control_last = msg_id
                try:
                    op = json.loads(fields[b"j"])
                except (KeyError, ValueError):
                    continue
                self.handle_control(op)

    def handle_control(self, op: Mapping) -> dict:
        op_id, cmd, args = op.get("id"), op.get("cmd"), op.get("args") or {}
        after = None
        sent = op.get("wall")
        if isinstance(sent, (int, float)) and time.time() - sent > self.orch.control_stale_s:
            # the API stopped waiting long ago and told the visitor to retry: acting now would surprise them
            _log(f"dropping stale control {cmd!r} ({time.time() - sent:.1f}s old)")
            return {"ok": False, "status": 503, "error": "stale", "message": "Request expired."}
        handlers = {"start": self._ctl_start, "pause": self._ctl_pause, "resume": self._ctl_resume,
                    "speed": self._ctl_speed, "reset": self._ctl_reset, "availability": self._ctl_availability}
        try:
            if cmd not in handlers:
                raise ctl.ControlError(400, "unknown_command", f"Unknown command {cmd!r}.")
            result, after = handlers[cmd](args)
            reply = {"ok": True, "result": result}
        except ctl.ControlError as exc:
            reply = {"ok": False, "status": exc.status, "error": exc.code, "message": exc.message, **exc.extra}
        except Exception as exc:  # never let a bad request take the fleet down
            _log(f"control {cmd!r} failed: {exc!r}")
            reply = {"ok": False, "status": 500, "error": "internal", "message": "Something went wrong in the simulator."}
        self._publish_snapshot(force=True)  # readers see the new state as soon as the reply lands
        if op_id:
            pipe = self.r.pipeline(transaction=False)
            pipe.lpush(self.keys.reply(op_id), json.dumps(reply))
            pipe.expire(self.keys.reply(op_id), 30)
            pipe.execute()
        if after is not None:
            after()
        return reply

    def _ctl_start(self, args: Mapping):
        run_id = self.prepare_run(args)
        return {"run_id": run_id, "settings": self.public_settings()}, self.init_run

    def _ctl_pause(self, _args):
        run = self.run
        if run is None or run.status not in ("running", "paused"):
            raise ctl.ControlError(409, "no_active_run", "There's no replay running to pause.")
        if run.status == "running":
            run.status = self.status = "paused"
            self._emit("run_paused", "info", f"Replay paused at {ctl.local_clock(self._sim_ts())} (Houston time).")
        return {"status": "paused"}, None

    def _ctl_resume(self, _args):
        run = self.run
        if run is None or run.status not in ("running", "paused"):
            raise ctl.ControlError(409, "no_active_run", "There's no paused replay to resume.")
        if run.status == "paused":
            run.status = self.status = "running"
            run.next_tick_at = time.monotonic()
            self._emit("run_resumed", "info", "Replay resumed.")
        return {"status": "running"}, None

    def _ctl_speed(self, args: Mapping):
        speed = ctl.parse_speed(args.get("speed_tps"), allow_max=self.allow_max_speed)
        if self.run is not None:
            self.run.speed = None if speed == "max" else float(speed)
            self.run.settings["speed_tps"] = speed
            self.run.next_tick_at = time.monotonic()
            self._emit("speed_changed", "info", f"Speed changed to {ctl.speed_phrase(speed)}.", detail={"speed_tps": speed})
        self._snapshot_dirty = True
        return {"speed_tps": speed}, None

    def _ctl_reset(self, _args):
        self._retire_run()
        self.status = "idle"
        return {"status": "idle"}, self.reset

    def _ctl_availability(self, args: Mapping):
        detail = self.availability.apply(args, self.owner, self.n_workers)
        changed = detail.pop("changed_idx")
        if len(changed):
            pipe = self.r.pipeline(transaction=False)
            for g in changed.tolist():
                pipe.hset(self.keys.battery(self.layout.ids[g]), F_AVAIL, int(not self.availability.offline[g]))
            pipe.execute()
        summary = self.availability.summary()
        next_ts = self._next_tick_ts()
        self._emit("availability_changed", "info", ctl.availability_message(detail, summary),
                   detail={k: v for k, v in detail.items()} | {"available": summary["available"]})
        self._snapshot_dirty = True
        return {"applied_from_tick_utc": to_utc_iso(next_ts), "affected": detail["affected"],
                "available": summary["available"], "total": summary["total"]}, None

    # --- runs ---------------------------------------------------------------------------------

    def public_settings(self) -> dict:
        s = dict(self.run.settings) if self.run else ctl.default_settings(self.cfg, self.raw, self.N)
        return {k: s[k] for k in ("preset", "start_utc", "speed_tps", "strategy", "start_charge_level", "fleet_size",
                                  "power_kw_range", "capacity_kwh_range", "locations")}

    def prepare_run(self, args: Mapping, *, fixed_targets: Mapping | None = None, max_ticks: int | None = None,
                    internal: bool = False) -> str:
        """Validate settings and replace any current run (status 'starting'). init_run() does the work."""
        settings = ctl.resolve_settings(args, self.cfg, self.raw, fleet_size=self.N,
                                        allow_max=self.allow_max_speed, allow_internal=internal)
        self._retire_run()
        speed = settings["speed_tps"]
        start = ctl.parse_utc(settings["start_utc"])
        run = RunState(run_id=str(uuid.uuid4()), settings=settings, start_ts=int(start.timestamp()),
                       speed=None if speed == "max" else float(speed), max_ticks=max_ticks)
        run.fixed = fixed_targets is not None
        run.fixed_targets = fixed_targets
        self.run = run
        self.status = "starting"
        self._snapshot_dirty = True
        return run.run_id

    def init_run(self) -> bool:
        """Load prices and strategies, initialise every battery for the run, wait for workers' 'ready'."""
        run = self.run
        if run is None:
            return False
        s = run.settings
        start = datetime.fromtimestamp(run.start_ts, UTC)
        try:
            prices = PriceWindow(price_feed(snapshot_years(start)), self.layout.locations, run.start_ts,
                                 lag=self.lag, window=self.window, warmup_s=self.warmup_s, tick_s=self.tick_s)
        except Exception as exc:
            if not run.fixed:
                _log(f"could not load prices: {exc!r}")
                self.run, self.status = None, "idle"
                self._emit("run_started", "warn", "The replay couldn't start because its price data couldn't be loaded.",
                           detail={"error": repr(exc), "price_source": os.environ.get("PRICE_SOURCE", "snapshot")})
                return False
            prices = None
        run.prices = prices
        if run.fixed:
            run.decide = FixedTargets(run.fixed_targets, prices)
            run.strategy_name = "fixed"
            if run.max_ticks is None:
                run.max_ticks = run.decide.length
        else:
            strategies, name, warning = build_strategies(s["strategy"], self.cfg, self.layout.locations,
                                                         start_ts=run.start_ts, tick_s=self.tick_s)
            run.decide = StrategyTargets(strategies, prices, self.lag, self.tick_s)
            run.strategy_name, run.strategy_warning = name, warning
        run.ledger = BacktestLedger({loc: int(n) for loc, n in zip(self.layout.locations, self.layout.counts)})
        run.initialized = np.zeros(self.N, dtype=bool)
        soc0 = float(s["start_charge_level"])
        self.soc[:] = soc0
        self.soc_k[:] = -1
        self.kw[:] = 0.0
        self.tick_log = []
        self.journal = {}
        pipe = self.r.pipeline(transaction=False)
        pipe.delete(self.keys.ticks, self.keys.dead_letter, self.keys.dead_letter_count)
        pipe.set(self.keys.kw, np.zeros(self.N, dtype=np.float32).tobytes())
        pipe.execute()
        if any(h.status == STARTING for h in self.workers):  # e.g. right after a reset
            self.wait_workers_up(self.orch.spawn_timeout_s)
        live = [h for h in self.workers if h.status in (HEALTHY, SUSPECT)]
        pipe = self.r.pipeline(transaction=False)
        for h in self.workers:
            h.inflight.clear()
            h.pending_load = np.empty(0, dtype=np.int32)
            h.pending_init = np.empty(0, dtype=bool)
            h.ready_run = None
            h.last_tick_ts = None
        for h in live:
            idx = h.owned
            pipe.xadd(self.keys.commands(h.worker_id), {"m": pack({
                "t": "load", "run": run.run_id, "n": self.N, "dt_h": self.dt_h, "soc_min": self.soc_min,
                "soc_max": self.soc_max, "load": {"idx": i32(idx), "ids": [self.layout.ids[g] for g in idx.tolist()],
                                                  "init": u8(np.ones(len(idx))), "soc0": soc0}})},
                maxlen=self.orch.maxlen["commands"], approximate=True)
        pipe.execute()
        deadline = time.monotonic() + self.orch.load_timeout_s
        while time.monotonic() < deadline and any(h.ready_run != run.run_id for h in live if h.status != DEAD):
            self._read_telemetry(200, None, None)
            self._supervise()
        for h in live:
            if h.status != DEAD and h.ready_run != run.run_id:
                h.misses += 1
                h.status = SUSPECT
                self._emit("worker_suspect", "warn", f"Controller {h.worker_id + 1} is slow to get ready, so its "
                           "batteries wait while the others start.",
                           detail={"worker_id": h.worker_id, "controller": h.worker_id + 1, "phase": "load"})
        run.status = self.status = "running"
        run.next_tick_at = time.monotonic()
        where = ctl.local_clock(run.start_ts)
        method = ctl.STRATEGY_LABELS.get(run.strategy_name, (run.strategy_name,))[0]
        msg = f"Replaying real prices from {where} (Houston time) at {ctl.speed_phrase(s['speed_tps'])}, using the {method.lower()}."
        severity = "info"
        if run.strategy_warning:
            msg = (f"Replaying real prices from {where} (Houston time) with the simple rule, because the AI forecast "
                   "isn't available yet.")
            severity = "warn"
        self._emit("run_started", severity, msg, sim_ts=run.start_ts, detail={
            "run_id": run.run_id, "settings": self.public_settings(), "strategy_effective": run.strategy_name,
            "fallback_reason": run.strategy_warning})
        self._publish_snapshot(force=True)
        return True

    def start(self, settings: Mapping | None = None, **kwargs) -> str:
        """prepare_run + init_run in one call (tests, bench, parity helper)."""
        run_id = self.prepare_run(settings or {}, **kwargs)
        if not self.init_run():
            raise RuntimeError("run failed to start")
        return run_id

    def _retire_run(self) -> None:
        if self.run is not None and self.run.ticks_done:
            self.last_run = {"run_id": self.run.run_id, "totals": self._totals()}
        self.run = None

    def _totals(self) -> dict:
        run = self.run
        return {"revenue_usd": round(run.revenue_usd, 6) if run else 0.0,
                "sold_kwh": round(run.sold_kwh, 6) if run else 0.0,
                "bought_kwh": round(run.bought_kwh, 6) if run else 0.0,
                "ticks": run.ticks_done if run else 0}

    def reset(self) -> None:
        """Stop the run, restart all workers (including killed ones), bring every battery back online."""
        self._retire_run()
        self.status = "idle"
        self._stop_workers()
        self.availability.clear()
        self.owner = self.layout.default_owners()
        self.soc[:] = self.cfg.battery.soc_init
        self.kw[:] = 0.0
        k = self.keys
        names = [k.telemetry, k.ticks, k.dead_letter, k.dead_letter_count, k.workers, k.kw]
        for w in range(self.n_workers):
            names += [k.commands(w), k.hb(w)]
        self.r.delete(*names)
        self._tele_last = "0-0"
        self._spawn_all()
        self._write_all_hashes(self.cfg.battery.soc_init)
        self._emit("run_reset", "good", f"Simulation reset: all {self.n_workers} controllers were restarted and every "
                   "battery is back online.", detail={"workers": self.n_workers})
        self._publish_snapshot(force=True)

    # --- the loop -------------------------------------------------------------------------------

    def loop_once(self) -> None:
        self._supervise()
        run = self.run
        if run is not None and run.status == "running" and time.monotonic() >= run.next_tick_at:
            self.step()
            self._schedule_next()
        self._publish_snapshot()
        if run is not None and run.status == "running" and self.run is run:
            wait_ms = int(max(0.0, run.next_tick_at - time.monotonic()) * 1000)
            wait_ms = min(wait_ms, 200)
        else:
            wait_ms = 200
        self.poll_control(wait_ms)

    def _schedule_next(self) -> None:
        run = self.run
        if run is None or run.status != "running":
            return
        now = time.monotonic()
        if run.speed is None:
            run.next_tick_at = now
            return
        period = 1.0 / run.speed
        run.next_tick_at += period
        if run.next_tick_at < now - period:  # fell behind: don't burst to catch up
            run.next_tick_at = now

    def serve_until(self, predicate: Callable[[], bool], timeout_s: float) -> bool:
        """Drive the real loop (pacing, supervision, control) until predicate() or timeout."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return True
            self.loop_once()
        return predicate()

    def deadline_s(self) -> float:
        """min(cap, fraction x tick period), but never under the floor (250 ms at 1-2 ticks/s, 100 ms at 5-10)."""
        run = self.run
        if run is None or run.speed is None:
            return self.orch.max_speed_deadline_ms / 1000.0
        o = self.orch
        return min(o.deadline_cap_ms / 1000.0, max(o.deadline_floor_ms / 1000.0, o.deadline_fraction / run.speed))

    def step(self) -> dict | None:
        """One tick: decide, allocate, dispatch, barrier, account, detect failures, recover."""
        run = self.run
        if run is None or run.status not in ("running", "paused"):
            return None
        k = run.k
        ts = run.start_ts + k * self.tick_s
        if (run.max_ticks is not None and k >= run.max_ticks) or (not run.fixed and run.prices.ended(ts)):
            run.status = self.status = "finished"
            end = "the end of the requested replay" if run.max_ticks is not None else "the end of the loaded price data"
            self._emit("run_finished", "info", f"The replay reached {end} ({ctl.local_clock(self._sim_ts())}, "
                       f"Houston time) after {ctl.fmt_int(run.ticks_done)} five-minute steps.",
                       detail={"run_id": run.run_id, "ticks": run.ticks_done, "revenue_usd": round(run.revenue_usd, 2)})
            self._publish_snapshot(force=True)
            return None
        if run.prices is not None:
            run.prices.ensure(ts)
        healthy = self._healthy_owner_mask()
        commandable = healthy & ~self.availability.offline
        setpoints = np.zeros(self.N)
        feasible: dict[str, float] = {}
        decisions: dict[str, Decision] = {}
        for li, loc in enumerate(self.layout.locations):
            sl = self.loc_idx[li]
            cm = commandable[sl]
            h_kw = fleet_headroom_kw(self.power[sl], cm)
            fsoc = fleet_soc(self.soc[sl], self.cap[sl], cm)
            dec = self._decide(run, k, ts, loc, fsoc, h_kw)
            raw = dec.raw_kw if math.isfinite(dec.raw_kw) else 0.0
            raw = min(max(raw, -h_kw), h_kw)
            d, c = headroom_kw(self.soc[sl], self.cap[sl], self.power[sl], self.eff[sl], cm, self.dt_h,
                               self.soc_min, self.soc_max)
            f, sp = allocate(raw, d, c)
            setpoints[sl] = sp
            feasible[loc] = f
            decisions[loc] = dec

        t0 = time.monotonic()
        sent = self._dispatch(run, k, ts, setpoints)
        reports = self._collect(k, {h.worker_id for h in sent}, t0 + self.deadline_s())
        barrier_ms = (time.monotonic() - t0) * 1000.0

        # Grid-side energy is metered from each battery's SoC change (grid_kwh_from_soc_change, as the
        # skeleton and the backtest meter it) and summed over the location's whole array in index order,
        # so the ledger sees bit-identical kWh. A cohort that didn't report moved 0.
        delivered = np.zeros(self.N)
        kwh_arr = np.zeros(self.N)
        for rep in reports.values():
            idx = np.frombuffer(rep["idx"], dtype=np.int32)
            kw = np.frombuffer(rep["kw"], dtype=np.float64)
            delivered[idx] = kw
            kwh_arr[idx] = self._metered_kwh(idx, np.frombuffer(rep["soc"], dtype=np.float64))
            self._update_soc(idx, np.frombuffer(rep["soc"], dtype=np.float64), k)
            if self.journal_on:
                self.journal.setdefault(k, []).append((idx.copy(), kw.copy()))
        self.kw = delivered
        kwh_by_loc = {loc: float(kwh_arr[self.loc_idx[li]].sum()) for li, loc in enumerate(self.layout.locations)}
        actual = {loc: kwh / self.dt_h for loc, kwh in kwh_by_loc.items()}
        revenue = {}
        for loc in self.layout.locations:  # config order, as the backtest fills its ledger
            tick = decisions[loc].now
            kwh = kwh_by_loc[loc]
            if tick is not None:
                run.ledger.record(tick, kwh)
                revenue[loc] = kwh * tick.price_usd_mwh / 1000.0
            else:
                revenue[loc] = 0.0
            self._add_totals(run, kwh, revenue[loc])


        newly_dead = []
        for h in sent:
            if h.worker_id in reports:
                h.last_tick_ts = ts
                if h.status == SUSPECT:
                    self._snapshot_dirty = True
                h.misses = 0
                if h.status != DEAD:
                    h.status = HEALTHY
            elif h.status != DEAD:
                h.misses += 1
                if h.misses >= self.orch.miss_threshold:
                    newly_dead.append(h)
                elif h.status != SUSPECT:
                    h.status = SUSPECT
                    self._snapshot_dirty = True
                    self._emit("worker_suspect", "warn",
                               f"Controller {h.worker_id + 1} didn't answer in time, so its batteries are paused while "
                               f"the other controllers cover for it.", sim_ts=ts,
                               detail={"worker_id": h.worker_id, "controller": h.worker_id + 1, "pid": h.pid,
                                       "tick_index": k, "deadline_ms": round(self.deadline_s() * 1000, 1)})
        for h in newly_dead:
            self._declare_dead(h, "missed_deadlines", k)
        self._supervise(force=True)  # heartbeat expiry, whichever comes first

        run.k += 1
        run.ticks_done += 1
        run.last_ts = ts
        self._snapshot_dirty = True  # totals moved: keep the snapshot within ~250 ms of the ticks
        record = self._tick_record(run, k, ts, decisions, feasible, actual, revenue)
        pipe = self.r.pipeline(transaction=False)
        pipe.xadd(self.keys.ticks, {"run": run.run_id, "k": k, "j": json.dumps(record)},
                  maxlen=self.orch.maxlen["ticks"], approximate=True)
        pipe.set(self.keys.kw, delivered.astype(np.float32).tobytes())
        pipe.execute()
        self._tick_events(run, k, ts, decisions, feasible)
        self.tick_log.append({**record, "k": k, "barrier_ms": round(barrier_ms, 2),
                              "reported": len(reports), "sent": len(sent),
                              "raw_kw": {loc: dec.raw_kw for loc, dec in decisions.items()},  # unrounded, for parity
                              "feasible_exact": dict(feasible), "kwh_exact": kwh_by_loc})
        if len(self.tick_log) > 20000:  # serve mode runs for hours: keep memory bounded
            del self.tick_log[:10000]
        for hook in list(self.tick_hooks):
            hook(self.tick_log[-1])
        self._publish_snapshot()
        return record

    def _decide(self, run: RunState, k, ts, loc, fsoc, h_kw) -> Decision:
        try:
            return run.decide(k, ts, loc, fsoc, h_kw)
        except Exception as exc:  # a broken strategy must not stop the fleet: fall back to bands
            _log(f"strategy for {loc} failed at tick {k}: {exc!r}; switching to bands")
            if isinstance(run.decide, StrategyTargets):
                fallback, _, _ = build_strategies("bands", self.cfg, [loc])
                run.decide.strategies[loc] = fallback[loc]
                run.strategy_warning = run.strategy_warning or repr(exc)
                self._emit("action_changed", "warn", f"The decision method for the {ctl.area(loc)} failed, so the "
                           "simple rule took over.", sim_ts=ts, detail={"location": loc, "error": repr(exc)})
                return run.decide(k, ts, loc, fsoc, h_kw)
            return Decision(0.0, None, None)

    def _healthy_owner_mask(self) -> np.ndarray:
        status_ok = np.zeros(self.n_workers + 1, dtype=bool)  # index -1 (orphan) maps to the last slot
        for h in self.workers:
            status_ok[h.worker_id] = h.status == HEALTHY
        return status_ok[self.owner]

    def _dispatch(self, run: RunState, k: int, ts: int, setpoints: np.ndarray) -> list[WorkerHandle]:
        av = (~self.availability.offline).astype(np.uint8)
        targets = [h for h in self.workers if h.status in (HEALTHY, SUSPECT) and (len(h.owned) or len(h.pending_load))]
        payloads = []
        for h in targets:
            idx = h.owned
            msg = {"t": "tick", "run": run.run_id, "k": k, "ts": ts, "idx": i32(idx), "sp": f64(setpoints[idx]),
                   "av": u8(av[idx])}
            if len(h.pending_load):
                msg["load"] = {"idx": i32(h.pending_load), "ids": [self.layout.ids[g] for g in h.pending_load.tolist()],
                               "init": u8(h.pending_init), "soc0": float(run.settings["start_charge_level"])}
            payloads.append((h, pack(msg), idx))
        for attempt in range(1, 4):
            try:
                pipe = self.r.pipeline(transaction=False)
                for h, payload, _ in payloads:
                    pipe.xadd(self.keys.commands(h.worker_id), {"m": payload}, maxlen=self.orch.maxlen["commands"],
                              approximate=True)
                pipe.execute()
                break
            except Exception as exc:  # a partial pipeline re-sent is a duplicate batch: workers dedupe it
                _log(f"dispatch of tick {k} failed (attempt {attempt}): {exc!r}")
                time.sleep(0.01 * attempt)
        else:
            entries = []
            for h, _, idx in payloads:
                entries += self._dead_letters_for(run, h.worker_id, idx, setpoints[idx], ts, "max_retries")
            try:
                write_dead_letters(self.r, self.keys, entries, self.orch.maxlen["dead_letter"])
            except Exception:
                pass
            return []
        for h, _, idx in payloads:
            h.inflight[k] = Inflight(ts, idx, setpoints[idx].copy())
            if len(h.pending_load):
                h.pending_load = np.empty(0, dtype=np.int32)
                h.pending_init = np.empty(0, dtype=bool)
        return [h for h, _, _ in payloads]

    def _collect(self, k: int, expected: set, deadline: float) -> dict:
        got: dict[int, dict] = {}
        while not expected.issubset(got):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._read_telemetry(None, k, got)
                break
            self._read_telemetry(max(1, int(remaining * 1000)), k, got)
        return {w: rep for w, rep in got.items() if w in expected}

    def _read_telemetry(self, block_ms: int | None, k_now: int | None, got: dict | None) -> int:
        try:
            resp = self.r.xread({self.keys.telemetry: self._tele_last}, block=block_ms, count=None)
        except Exception as exc:
            _log(f"telemetry read failed: {exc!r}")
            time.sleep(0.01)
            return 0
        n = 0
        for _name, entries in resp or []:
            for msg_id, fields in entries:
                self._tele_last = msg_id
                try:
                    rep = unpack(fields[b"m"])
                except Exception:
                    continue
                self._on_report(rep, k_now, got)
                n += 1
        return n

    def _on_report(self, rep: dict, k_now: int | None, got: dict | None) -> None:
        run = self.run
        if run is None or rep.get("run") != run.run_id:
            return
        w = rep.get("w")
        if not isinstance(w, int) or not 0 <= w < len(self.workers):
            return
        h = self.workers[w]
        if rep.get("e") != h.epoch:
            return
        if rep.get("t") == "ready":
            h.ready_run = run.run_id
            if run.initialized is not None:
                run.initialized[h.owned] = True
            return
        if rep.get("t") != "tick":
            return
        kr = int(rep["k"])
        infl = h.inflight.pop(kr, None)
        if infl is None:
            return  # a duplicate report of a tick already accounted for
        if run.initialized is not None:  # a battery the worker skipped reports NaN: not initialised by it
            idx = np.frombuffer(rep["idx"], dtype=np.int32)
            run.initialized[idx[np.isfinite(np.frombuffer(rep["soc"], dtype=np.float64))]] = True
        if got is not None and kr == k_now:
            got[w] = rep
        else:
            self._account_late(run, rep, infl)

    def _account_late(self, run: RunState, rep: dict, infl: Inflight) -> None:
        """Telemetry for a tick already closed: the energy really moved, so book it at that tick's price."""
        idx = np.frombuffer(rep["idx"], dtype=np.int32)
        kw = np.frombuffer(rep["kw"], dtype=np.float64)
        soc_new = np.frombuffer(rep["soc"], dtype=np.float64)
        kwh_each = self._metered_kwh(idx, soc_new)
        self._update_soc(idx, soc_new, int(rep["k"]))
        if self.journal_on:
            self.journal.setdefault(int(rep["k"]), []).append((idx.copy(), kw.copy()))
        for li, loc in enumerate(self.layout.locations):
            kwh = float(kwh_each[self.layout.loc_of[idx] == li].sum())
            tick = run.prices.at(loc, infl.ts) if run.prices is not None else None
            usd = 0.0
            if tick is not None:
                run.ledger.record(tick, kwh)
                usd = kwh * tick.price_usd_mwh / 1000.0
            self._add_totals(run, kwh, usd)

    @staticmethod
    def _add_totals(run: RunState, kwh: float, usd: float) -> None:
        run.revenue_usd += usd
        if kwh > 0:
            run.sold_kwh += kwh
        elif kwh < 0:
            run.bought_kwh -= kwh

    def _metered_kwh(self, idx: np.ndarray, soc_new: np.ndarray) -> np.ndarray:
        """Grid-side kWh per reported battery from its SoC change (0 where the worker reported no SoC)."""
        prev = self.soc[idx]
        after = np.where(np.isfinite(soc_new), soc_new, prev)
        return grid_kwh_from_soc_change(prev, after, self.cap[idx], self.eff[idx])

    def _update_soc(self, idx: np.ndarray, soc: np.ndarray, k: int) -> None:
        ok = np.isfinite(soc) & (k >= self.soc_k[idx])
        self.soc[idx[ok]] = soc[ok]
        self.soc_k[idx[ok]] = k

    # --- failure handling -----------------------------------------------------------------------

    def _declare_dead(self, h: WorkerHandle, reason: str, k: int | None) -> None:
        if h.status == DEAD:
            return
        t0 = time.perf_counter()
        h.status = DEAD
        h.dead_reason = reason
        self.r.set(self.keys.lease(h.worker_id), f"dead:{h.epoch}")  # fence the zombie before anyone adopts
        self._read_telemetry(None, None, None)  # book whatever it committed before the fence
        expired = sorted(h.inflight.items())
        h.inflight.clear()
        cohort = np.sort(np.concatenate([h.owned, h.pending_load])).astype(np.int32)
        h.owned = np.empty(0, dtype=np.int32)
        h.pending_load = np.empty(0, dtype=np.int32)
        h.pending_init = np.empty(0, dtype=bool)
        adopters = [x for x in self.workers if x.status == HEALTHY]
        self._reassign(cohort, adopters, h.worker_id)
        handover_ms = (time.perf_counter() - t0) * 1000.0
        run = self.run
        sim_ts = run.start_ts + (k if k is not None else run.k) * self.tick_s if run else None
        why = {"missed_deadlines": f" after missing {self.orch.miss_threshold} check-ins in a row",
               "heartbeat_expired": f" (no heartbeat for {self.orch.hb_ttl_s:g} seconds)",
               "no_heartbeat_at_start": " while starting up"}.get(reason, "")
        self._emit("worker_dead", "warn", f"Controller {h.worker_id + 1} stopped responding{why}.", sim_ts=sim_ts,
                   detail={"worker_id": h.worker_id, "controller": h.worker_id + 1, "pid": h.pid, "reason": reason,
                           "tick_index": k, "unconfirmed_ticks": [kk for kk, _ in expired], "exit_code": h.exit_code})
        if len(cohort):
            others = len(adopters)
            when = "under 1 ms" if handover_ms < 1 else f"{handover_ms:.0f} ms"
            if others:
                msg = (f"Controller {h.worker_id + 1}'s {ctl.fmt_int(len(cohort))} batteries were handed to the other "
                       f"{others} controller{'s' if others != 1 else ''} in {when}.")
                self._emit("worker_adopted", "good", msg, sim_ts=sim_ts, detail={
                    "worker_id": h.worker_id, "controller": h.worker_id + 1, "batteries": int(len(cohort)),
                    "adopted_by": [a.worker_id for a in adopters], "ms": round(handover_ms, 3),
                    "declared_at_tick_index": k, "reassigned_for_tick_index": (k + 1) if k is not None else None})
        if run is not None and expired:
            entries = []
            for kk, infl in expired:
                entries += self._dead_letters_for(run, h.worker_id, infl.idx, infl.sp, infl.ts, "expired_owner_dead")
            write_dead_letters(self.r, self.keys, entries, self.orch.maxlen["dead_letter"])
        self._snapshot_dirty = True
        self._publish_snapshot(force=True)

    def _reassign(self, cohort: np.ndarray, adopters: list[WorkerHandle], from_worker: int | None) -> None:
        """Round-robin the cohort over healthy workers; they HMGET it lazily with their next batch."""
        if len(cohort) == 0:
            return
        if not adopters:
            self.owner[cohort] = -1  # orphans: retried as soon as a worker is healthy
            return
        run = self.run
        writes = []
        n = len(adopters)
        for j, a in enumerate(adopters):
            part = cohort[j::n]
            if not len(part):
                continue
            self.owner[part] = a.worker_id
            a.owned = np.sort(np.concatenate([a.owned, part])).astype(np.int32)
            a.pending_load = np.concatenate([a.pending_load, part]).astype(np.int32)
            init = ~run.initialized[part] if (run is not None and run.initialized is not None) else np.ones(len(part), bool)
            a.pending_init = np.concatenate([a.pending_init, init]).astype(bool)
            if from_worker is not None and from_worker not in a.adopted_from:
                a.adopted_from.append(from_worker)
            tail = b"$5\r\nowner\r\n$%d\r\n%d\r\n$11\r\nowner_epoch\r\n$%d\r\n%d\r\n" % (
                len(str(a.worker_id)), a.worker_id, len(str(a.epoch)), a.epoch)
            for g in part.tolist():
                key = self._bkey[g]
                writes.append(b"*6\r\n$4\r\nHSET\r\n$%d\r\n%s\r\n%s" % (len(key), key, tail))
        self._send_raw(writes)

    def _send_raw(self, commands: list[bytes]) -> None:
        """Pipeline pre-encoded RESP commands on one pooled connection (the hot bulk-write path)."""
        if not commands:
            return
        conn = self.r.connection_pool.get_connection()
        try:
            conn.send_packed_command([b"".join(commands)])
            for _ in range(len(commands)):
                conn.read_response()
        except BaseException:
            conn.disconnect()
            raise
        finally:
            self.r.connection_pool.release(conn)

    def _reassign_orphans(self) -> None:
        orphans = np.flatnonzero(self.owner < 0).astype(np.int32)
        adopters = [x for x in self.workers if x.status == HEALTHY]
        if len(orphans) and adopters:
            self._reassign(orphans, adopters, None)

    def _dead_letters_for(self, run: RunState, worker_id: int, idx, sp, ts: int, reason: str) -> list[dict]:
        ids = [self.layout.ids[g] for g in np.asarray(idx).tolist()]
        factory = CommandIds(run.run_id)
        cids = factory.generate(factory.prefixes(ids), ts)
        return [dead_letter_entry(command_id=cids[j].decode(), battery_id=ids[j], worker_id=worker_id, tick_ts=ts,
                                  reason=reason, run_id=run.run_id, setpoint_kw=float(sp[j])) for j in range(len(ids))]

    # --- publishing -----------------------------------------------------------------------------

    def _sim_ts(self) -> int | None:
        run = self.run
        if run is None:
            return None
        return run.last_ts if run.last_ts is not None else run.start_ts

    def _next_tick_ts(self) -> int | None:
        run = self.run
        return None if run is None else run.start_ts + run.k * self.tick_s

    def _tick_record(self, run, k, ts, decisions, feasible, actual, revenue) -> dict:
        locs = self.layout.locations
        target_fleet = float(sum(feasible.values()))
        actual_fleet = float(sum(actual.values()))
        floor = 0.005 * self.nameplate_kw
        err = abs(actual_fleet - target_fleet) / max(abs(target_fleet), floor) * 100.0
        eps = 1e-6
        action = {loc: ("sell" if feasible[loc] > eps else "charge" if feasible[loc] < -eps else "hold") for loc in locs}
        charge = {}
        for li, loc in enumerate(locs):
            sl = self.loc_idx[li]
            charge[loc] = round(float((self.soc[sl] * self.cap[sl]).sum() / self.cap[sl].sum()), 5)
        charge["fleet"] = round(float((self.soc * self.cap).sum() / self.cap.sum()), 5)
        healthy = sum(1 for h in self.workers if h.status == HEALTHY)

        def price(t):
            return None if t is None else round(float(t.price_usd_mwh), 4)

        return {
            "ts_utc": to_utc_iso(ts),
            "ts_local": ctl.to_local_iso(ts),
            "price": {loc: price(decisions[loc].now) for loc in locs},
            "decision_price": {loc: price(decisions[loc].published) for loc in locs},
            "action": action,
            "target_kw": {**{loc: round(feasible[loc], 3) for loc in locs}, "fleet": round(target_fleet, 3)},
            "actual_kw": {**{loc: round(actual[loc], 3) for loc in locs}, "fleet": round(actual_fleet, 3)},
            "error_pct": round(err, 6),
            "revenue_usd": {**{loc: round(revenue[loc], 6) for loc in locs}, "fleet": round(sum(revenue.values()), 6)},
            "charge_level": charge,
            "healthy_workers": healthy,
        }

    def _tick_events(self, run: RunState, k: int, ts: int, decisions: dict, feasible: dict) -> None:
        if run.fixed:
            return
        for loc, dec in decisions.items():  # price spikes, once per episode
            if dec.now is None:
                continue
            p = dec.now.price_usd_mwh
            if p >= self.orch.price_spike_usd_mwh and not run.spike_active.get(loc):
                run.spike_active[loc] = True
                self._emit("price_spike", "info", f"The price in the {ctl.area(loc)} jumped to {ctl.price_phrase(p)}.",
                           sim_ts=ts, detail={"location": loc, "price_usd_mwh": p})
            elif p < 0.5 * self.orch.price_spike_usd_mwh:
                run.spike_active[loc] = False
        eps = 1e-6
        actions = {loc: ("sell" if f > eps else "charge" if f < -eps else "hold") for loc, f in feasible.items()}
        if actions != run.announced_action and k - run.last_action_event_k >= self.orch.action_event_min_ticks:
            first = not run.announced_action
            run.announced_action = dict(actions)
            run.last_action_event_k = k
            if not first or any(a != "hold" for a in actions.values()):
                self._emit("action_changed", "info", ctl.action_message(actions), sim_ts=ts,
                           detail={"actions": actions, "target_kw": {loc: round(v, 1) for loc, v in feasible.items()}})

    def _emit(self, kind: str, severity: str, message: str, *, sim_ts: int | None = None, detail: dict | None = None):
        if sim_ts is None:
            sim_ts = self._sim_ts()
        event = ctl.make_event(kind, severity, message, sim_ts=sim_ts, detail=_jsonable(detail or {}))
        try:
            event_id = ctl.emit_event(self.r, self.keys, event, self.orch.maxlen["events"])
        except Exception as exc:
            _log(f"event {kind} not published: {exc!r}")
            event_id = None
        self.events.append({"id": event_id, **event})
        if len(self.events) > 2000:
            del self.events[:1000]
        return event

    def snapshot(self) -> dict:
        run = self.run
        return {
            "run_id": run.run_id if run else None,
            "status": self.status,
            "settings": self.public_settings(),
            "sim_time_utc": to_utc_iso(self._sim_ts()),
            "speed_tps": (run.settings["speed_tps"] if run else ctl.default_settings(self.cfg, self.raw, self.N)["speed_tps"]),
            "totals": self._totals(),
            "last_run": self.last_run,
            "workers": [h.public() for h in self.workers],
            "availability": self.availability.summary(),
            "strategy_effective": run.strategy_name if run else None,
            "tick_index": run.k if run else None,
            "next_tick_utc": to_utc_iso(self._next_tick_ts()),
            "coordinator_pid": os.getpid(),
            "updated_utc": now_utc_iso(),
        }

    def _publish_snapshot(self, force: bool = False) -> None:
        now = time.monotonic()
        interval = self.orch.snapshot_interval_s if self._snapshot_dirty else 1.0
        if not force and now - self._last_snapshot < interval:
            return
        self._last_snapshot = now
        self._snapshot_dirty = False
        try:
            self.r.set(self.keys.snapshot, json.dumps(self.snapshot()))
        except Exception as exc:
            _log(f"snapshot not published: {exc!r}")

    # --- inspection (tests, parity) ---------------------------------------------------------------

    def read_hash_state(self, fields=(F_SOC, F_TICK, F_CID, F_EPOCH, F_OWNER)) -> dict[str, list]:
        pipe = self.r.pipeline(transaction=False)
        for bid in self.layout.ids:
            pipe.hmget(self.keys.battery(bid), fields)
        rows = pipe.execute()
        return {f: [row[i] for row in rows] for i, f in enumerate(fields)}

    def read_hash_soc(self) -> np.ndarray:
        return np.array([float(x) for x in self.read_hash_state((F_SOC,))[F_SOC]])

    def replay_journal(self, soc0: float) -> np.ndarray:
        """SoC implied by applying every delivery the workers reported exactly once, in tick order,
        with src.sim.battery.step. Equal to the hashes iff nothing was lost or double-applied."""
        from src.sim.battery import step as physics_step

        soc = np.full(self.N, float(soc0))
        ticks = self.run.k if self.run else (max(self.journal) + 1 if self.journal else 0)
        for k in range(ticks):
            sp = np.zeros(self.N)
            for idx, kw in self.journal.get(k, []):
                sp[idx] = kw
            soc, _ = physics_step(soc, self.cap, self.power, self.eff, np.ones(self.N, dtype=bool), sp, self.dt_h,
                                  self.soc_min, self.soc_max)
        return soc


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return value


# --- parity helper ------------------------------------------------------------------------------------


def run_fleet_targets(
    targets_kw: Mapping[str, Sequence[float]],
    *,
    start_utc: str = "2025-05-16T05:00:00Z",
    soc_init: float = 0.5,
    fleet_size: int | None = None,
    workers: int | None = None,
    url: str | None = None,
    prefix: str | None = None,
    cfg: Config | None = None,
) -> dict:
    """Run len(targets) ticks through the real worker fleet (separate OS processes, Redis) with the
    given raw per-location fleet targets (kW, grid-side, + sell / - charge), no strategy.

    Each tick allocates exactly as live mode does (clip to feasible headroom, proportional split).
    Returns {"battery_ids", "soc" (final per-battery SoC read from the battery hashes, global order),
    "telemetry_soc" (the coordinator's view, must equal "soc"), "target_kw", "actual_kw" (per tick,
    per location), "ticks"}. The in-process equivalent is, per tick and location:
        d, c = headroom_kw(soc, ...); _, sp = allocate(target, d, c); soc, _ = step(soc, ..., sp, ...)
    """
    prefix = prefix if prefix is not None else f"parity:{uuid.uuid4().hex[:8]}:"
    co = Coordinator(url=url, prefix=prefix, fleet_size=fleet_size, workers=workers, cfg=cfg)
    try:
        co.boot()
        n = max((len(v) for v in targets_kw.values()), default=0)
        co.start({"start_charge_level": soc_init, "speed_tps": "max", "start_utc": start_utc},
                 fixed_targets=targets_kw, max_ticks=n, internal=True)
        rows = []
        while co.run is not None and co.run.status == "running":
            rec = co.step()
            if rec is None:
                break
            rows.append(rec)
        return {
            "battery_ids": list(co.layout.ids),
            "soc": co.read_hash_soc(),
            "telemetry_soc": co.soc.copy(),
            "target_kw": [r["target_kw"] for r in rows],
            "actual_kw": [r["actual_kw"] for r in rows],
            "ticks": len(rows),
        }
    finally:
        co.shutdown()
        if co.keys.prefix:
            co.cleanup_keys()


def run_fleet_replay(
    strategy: str,
    *,
    start_utc: str,
    ticks: int = 288,
    soc_init: float = 0.5,
    fleet_size: int | None = None,
    workers: int | None = None,
    url: str | None = None,
    prefix: str | None = None,
    cfg: Config | None = None,
    strict: bool = True,
) -> dict:
    """Replay `ticks` real ticks from start_utc through the real worker fleet, decided by `strategy`
    exactly as live mode decides (for parity with src.backtest):

    - fleet: generate_batteries + assign_locations (b00000.., contiguous blocks in config order);
    - one make_strategy(cfg.strategy, tick_seconds, name) per location, no extra context;
    - decision_tick(now, published) with published `decision_lag_ticks` back, history = the trailing
      window of prices before it, warm-up starting window_days before start_utc (as the skeleton);
    - grid-side kWh metered from the SoC change and recorded per tick, locations in config order.

    Returns {"battery_ids", "soc" (final SoC from the battery hashes, global order), "summary"
    (Ledger.summary()), "strategy_effective", "ticks", "locations": {loc: {"battery_ids", "soc_final",
    "delivered_kwh", "feasible_kw", "target_kw"}}} (per-tick arrays are unrounded). With strict=True a
    'forecast' run that had to fall back to bands raises RuntimeError instead of comparing apples to pears.
    """
    cfg = cfg or load_config()
    prefix = prefix if prefix is not None else f"parity:{uuid.uuid4().hex[:8]}:"
    orch = Orchestration.from_config(load_raw_config(), max_speed_deadline_ms=2000.0)
    co = Coordinator(url=url, prefix=prefix, fleet_size=fleet_size, workers=workers, cfg=cfg, orchestration=orch)
    try:
        co.boot()
        co.start({"start_utc": start_utc, "speed_tps": "max", "strategy": strategy, "start_charge_level": soc_init},
                 max_ticks=ticks, internal=True)
        if strict and co.run.strategy_name != strategy:
            raise RuntimeError(f"{strategy!r} could not run ({co.run.strategy_warning}); it fell back to "
                               f"{co.run.strategy_name!r}")
        while co.run is not None and co.run.status == "running":
            if co.step() is None:
                break
        log = co.tick_log
        soc = co.read_hash_soc()
        locations = {}
        for li, loc in enumerate(co.layout.locations):
            sl = co.layout.location_indices(li)
            locations[loc] = {
                "battery_ids": [co.layout.ids[g] for g in sl.tolist()],
                "soc_final": soc[sl],
                "delivered_kwh": np.array([r["kwh_exact"][loc] for r in log]),
                "feasible_kw": np.array([r["feasible_exact"][loc] for r in log]),
                "target_kw": np.array([r["raw_kw"][loc] for r in log]),
            }
        return {"battery_ids": list(co.layout.ids), "soc": soc, "summary": co.run.ledger.summary(),
                "strategy_effective": co.run.strategy_name, "ticks": len(log), "locations": locations}
    finally:
        co.shutdown()
        if co.keys.prefix:
            co.cleanup_keys()


# --- CLI -------------------------------------------------------------------------------------------


def _bench(args) -> int:
    prefix = args.prefix if args.prefix is not None else f"bench:{uuid.uuid4().hex[:6]}:"
    co = Coordinator(url=args.redis_url, prefix=prefix, fleet_size=args.fleet_size, workers=args.workers)
    try:
        t_boot = time.perf_counter()
        co.boot()
        boot_s = time.perf_counter() - t_boot
        speed = args.speed if args.speed == "max" else int(args.speed)
        settings = {"speed_tps": speed, "strategy": args.strategy, "start_charge_level": args.soc}
        if args.start_utc:
            settings["start_utc"] = args.start_utc
        else:
            settings["preset"] = args.preset
        t_init = time.perf_counter()
        co.start(settings, max_ticks=args.ticks, internal=True)
        init_s = time.perf_counter() - t_init
        killed = {}

        def kill_hook(rec):
            if args.kill_at is not None and rec["k"] == args.kill_at and not killed:
                victim = co.workers[args.kill_worker]
                os.kill(victim.pid, signal.SIGKILL)
                killed.update(worker_id=victim.worker_id, pid=victim.pid, after_tick=rec["k"])
                _log(f"SIGKILLed worker {victim.worker_id} (pid {victim.pid}) after tick {rec['k']}")

        co.tick_hooks.append(kill_hook)
        t0 = time.perf_counter()
        co.serve_until(lambda: co.run is None or co.run.status == "finished", timeout_s=args.timeout)
        wall = time.perf_counter() - t0
        log = co.tick_log
        errs = [r["error_pct"] for r in log]
        out = {
            "fleet_size": co.N, "workers": co.n_workers, "ticks": len(log), "speed": speed,
            "wall_s": round(wall, 3), "ticks_per_s": round(len(log) / wall, 2) if wall else None,
            "boot_s": round(boot_s, 2), "init_s": round(init_s, 2),
            "max_error_pct": round(max(errs), 6) if errs else None,
            "ticks_over_5pct": sum(1 for e in errs if e > 5.0),
            "barrier_ms_p50": float(np.percentile([r["barrier_ms"] for r in log], 50)) if log else None,
            "barrier_ms_max": max((r["barrier_ms"] for r in log), default=None),
            "revenue_usd": round(co.run.revenue_usd, 2) if co.run else None,
            "killed": killed or None,
        }
        if killed:
            ka = killed["after_tick"]
            out["error_pct_after_kill"] = [f"tick {r['k']}: {r['error_pct']:.3f}% ({r['healthy_workers']} healthy)"
                                           for r in log if ka < r["k"] <= ka + 5]
            out["events"] = [f"{e['kind']}: {e['message']}" for e in co.events if e["kind"].startswith("worker_")]
            out["dead_letter"] = int(co.r.get(co.keys.dead_letter_count) or 0)
        print(json.dumps(out, indent=2))
        return 0 if out["ticks_over_5pct"] == 0 or killed else 1
    finally:
        co.shutdown()
        if co.keys.prefix:
            co.cleanup_keys()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.sim.coordinator", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the coordinator (spawns and supervises the workers)")
    bench = sub.add_parser("bench", help="M2/M3 numbers: run N ticks, optionally SIGKILL a worker")
    for p in (serve, bench):
        p.add_argument("--redis-url", default=None)
        p.add_argument("--prefix", default=None)
        p.add_argument("--fleet-size", type=int, default=None)
        p.add_argument("--workers", type=int, default=None)
    serve.add_argument("--parent-pid", type=int, default=None)
    bench.add_argument("--ticks", type=int, default=288)
    bench.add_argument("--speed", default="max")
    bench.add_argument("--strategy", default="bands", choices=ctl.STRATEGIES)
    bench.add_argument("--preset", default="spike_evening")
    bench.add_argument("--start-utc", default=None)
    bench.add_argument("--soc", type=float, default=0.5)
    bench.add_argument("--kill-at", type=int, default=None, help="SIGKILL a worker right after this tick index")
    bench.add_argument("--kill-worker", type=int, default=7)
    bench.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args(argv)
    if args.cmd == "bench":
        return _bench(args)
    parent = args.parent_pid if getattr(args, "parent_pid", None) is not None else (
        int(os.environ["FLEET_PARENT_PID"]) if os.environ.get("FLEET_PARENT_PID") else None)
    env_int = lambda name: int(os.environ[name]) if os.environ.get(name) else None  # noqa: E731
    co = Coordinator(url=getattr(args, "redis_url", None), prefix=getattr(args, "prefix", None),
                     fleet_size=getattr(args, "fleet_size", None) or env_int("FLEET_SIZE"),
                     workers=getattr(args, "workers", None) or env_int("FLEET_WORKERS"),
                     parent_pid=parent, allow_max_speed=os.environ.get("FLEET_ALLOW_MAX_SPEED") == "1")

    def _term(signum, _frame):
        _log(f"signal {signum}: stopping")
        co.request_stop()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    co.boot(wait_healthy=False)
    threading.Thread(target=_preload_prices, args=(co,), name="price-preload", daemon=True).start()
    threading.Thread(target=_keep_forecasts_warm, args=(co,), name="forecast-warm", daemon=True).start()
    co.serve_forever()
    return 0


def _preload_prices(co: Coordinator) -> None:
    """Load the default preset's price year up front so the first Start is quick."""
    try:
        start = ctl.parse_utc(ctl.default_settings(co.cfg, co.raw, co.N)["start_utc"])
        price_feed(snapshot_years(start))
    except Exception as exc:
        _log(f"price preload skipped: {exc!r}")


def _keep_forecasts_warm(co: Coordinator, every_s: float = 0.8 * STRATEGY_CACHE_S) -> None:
    """Serve mode: (re)build the forecast strategies in the background, so a Start never waits for a
    year of forecasts to load from the database, and forecasts a job writes later are picked up."""
    while not co._stop:
        t0 = time.monotonic()
        try:
            _, name, warning = build_strategies("forecast", co.cfg, co.layout.locations, refresh=True)
            if warning:
                _log(f"forecast strategy not ready: {warning}")
            else:
                _log(f"forecast strategies loaded in {time.monotonic() - t0:.1f}s")
        except Exception as exc:
            _log(f"forecast warm-up failed: {exc!r}")
        deadline = time.monotonic() + every_s
        while not co._stop and time.monotonic() < deadline:
            time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
