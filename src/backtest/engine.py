"""The vectorized replay engine: one location's batteries through a run of 5-minute ticks.

Per tick it runs the live path's steps, in the same order and with the same shared code
(RUNLOG decisions 1, 4, 5 and "Strategy inputs"):

  1. Strategy.target_kw(decision_tick(now, published), fleet_soc, H, history), the frozen seam.
     published = the tick `lag` ticks back (sim.decision_lag_ticks); history = the prices
     strictly before `published`, trailing `history_ticks` (the M0 skeleton passes
     strategy.bands.window_days of ticks). H = fleet_headroom_kw, the symmetric headroom.
  2. src.sim.battery.headroom_kw, then src.sim.allocation.allocate: clip the target to the
     feasible directional headroom and split it in proportion to each battery's headroom.
  3. src.sim.battery.step on all of the location's batteries at once (numpy).
  4. Grid-side kWh metered from the SoC change (src.sim.battery.grid_kwh_from_soc_change,
     as the skeleton meters through read()), summed over the location's batteries. The
     caller records it in the Ledger per (tick, location) at that tick's RT price.

Steps 2-4 are `deliver`, which the live race (src/sim/race.py) calls too, so the shadow fleet
steps exactly like the backtest.

Shortcuts that change no number (tests/test_backtest_engine.py pins each one):
  - A hold (target 0) or a target with no feasible headroom skips steps 2-4: step() with
    all-zero setpoints returns the same SoC, and the meter reads 0.
  - H is computed once: nobody takes batteries offline during a backtest.
  - fleet_soc is recomputed only after the SoC has moved.
  - bands: `bands_targets` computes every tick's decision at once from batched
    np.percentile calls over the same windows the seam would see. It is bit-identical to
    calling BandsStrategy.target_kw per tick, and is used only for that exact class.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from src.config import BatteryConfig
from src.core.interfaces import PriceFeed, Strategy
from src.core.models import BatteryState, PriceTick
from src.ledger.backtest import BacktestLedger
from src.sim.allocation import allocate, fleet_headroom_kw
from src.sim.battery import fleet_soc, grid_kwh_from_soc_change, headroom_kw, step
from src.sim.decision import decision_tick
from src.sim.fleet import assign_locations, generate_batteries, location_counts  # noqa: F401 (location_counts re-exported)
from src.strategy.bands import BandsStrategy

UTC = timezone.utc
OPERATING_TZ = "America/Chicago"  # ERCOT operating days


# --- the fleet -----------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class Fleet:
    """One location's batteries as numpy arrays (read-only inputs; the replay copies the SoC)."""

    location: str
    battery_ids: tuple[str, ...]
    capacity_kwh: np.ndarray
    power_kw: np.ndarray
    efficiency: np.ndarray
    soc_init: np.ndarray
    available: np.ndarray

    @property
    def size(self) -> int:
        return len(self.battery_ids)

    @classmethod
    def from_states(cls, location: str, states: Sequence[BatteryState]) -> Fleet:
        def arr(values, dtype=np.float64):
            a = np.array(list(values), dtype=dtype)
            a.setflags(write=False)
            return a

        return cls(
            location=location,
            battery_ids=tuple(s.battery_id for s in states),
            capacity_kwh=arr(s.capacity_kwh for s in states),
            power_kw=arr(s.power_kw for s in states),
            efficiency=arr(s.efficiency for s in states),
            soc_init=arr(s.soc for s in states),
            available=arr((s.available for s in states), dtype=bool),
        )


def build_fleets(
    size: int, shares: Mapping[str, float], battery: BatteryConfig, seed: int, *, soc_init: float | None = None
) -> dict[str, Fleet]:
    """The seeded fleet (src.sim.fleet.generate_batteries), split into contiguous blocks.

    Locations take consecutive blocks of the battery list in config order
    (src.sim.fleet.assign_locations): at 20,000 batteries LZ_HOUSTON gets b00000-b06999,
    LZ_NORTH b07000-b14999, LZ_CPS b15000-b17599 and LZ_AEN b17600-b19999.
    Battery i has the same specs at any fleet size, so a smaller test fleet is a prefix.
    """
    states = generate_batteries(size, battery, seed, soc_init=soc_init)
    return {loc: Fleet.from_states(loc, block) for loc, block in assign_locations(states, shares).items()}


# --- prices ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TickSeries:
    """A location's 5-minute ticks: warm-up ticks first, then the replayed ones from `start`."""

    location: str
    ticks: list[PriceTick]
    start: int

    @property
    def prices(self) -> np.ndarray:
        return np.fromiter((t.price_usd_mwh for t in self.ticks), dtype=np.float64, count=len(self.ticks))

    @property
    def replayed(self) -> list[PriceTick]:
        return self.ticks[self.start:]


def local_midnight_utc(day, tz: str = OPERATING_TZ) -> datetime:
    """UTC instant of local midnight at the start of `day` (a date)."""
    return datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def year_bounds(year: int, tz: str = OPERATING_TZ) -> tuple[datetime, datetime]:
    """[start, end) of a calendar year in operating-day time, as UTC instants."""
    return local_midnight_utc(date(year, 1, 1), tz), local_midnight_utc(date(year + 1, 1, 1), tz)


def rt_intervals(location: str, start: datetime, end: datetime) -> tuple[object, str]:
    """Native 15-min RT prices from src.data.series, and the backend they came from.

    series reads Postgres when a database is configured, else the committed snapshot. If a
    database is configured but the Postgres series backend (M1) is not in this build, the
    snapshot of the same real data is read instead, and the backend name says so.
    """
    import os

    from src.data import series

    try:
        return series.rt_prices(location, start, end), series.backend()
    except NotImplementedError:
        old = os.environ.get("SERIES_BACKEND")
        os.environ["SERIES_BACKEND"] = "snapshot"
        try:
            return series.rt_prices(location, start, end), "snapshot (Postgres series backend not in this build)"
        finally:
            if old is None:
                os.environ.pop("SERIES_BACKEND", None)
            else:
                os.environ["SERIES_BACKEND"] = old


def feed_from_series(locations: Iterable[str], start: datetime, end: datetime):
    """PriceFeed over src.data.series RT prices. Returns (feed, backend name).

    Native 15-min rows go through InMemoryPriceFeed.from_intervals, the canonical forward
    fill: each interval's price is held for its own three ticks, never across a gap.
    """
    from src.feeds.memory import InMemoryPriceFeed

    rows: list[PriceTick] = []
    backends = set()
    for loc in locations:
        s, backend = rt_intervals(loc, start, end)
        backends.add(backend)
        rows.extend(PriceTick(ts.to_pydatetime(), loc, float(p), None, None) for ts, p in s.items())
    return InMemoryPriceFeed.from_intervals(rows), ", ".join(sorted(backends))


def load_ticks(
    feed: PriceFeed, locations: Iterable[str], start: datetime, end: datetime, warmup_start: datetime
) -> dict[str, TickSeries]:
    """Ticks in [warmup_start, end) per location, from any PriceFeed; replay starts at `start`."""
    out = {}
    for loc in locations:
        ticks = list(feed.ticks(warmup_start, end, loc))
        out[loc] = TickSeries(loc, ticks, bisect_left([t.ts for t in ticks], start))
    return out


# --- the replay ------------------------------------------------------------------------------


@dataclass
class LocationRun:
    """One location's replay. Arrays over replayed ticks have one entry per tick."""

    location: str
    target_kw: np.ndarray  # the strategy's output (before clipping)
    feasible_kw: np.ndarray  # after clipping to the feasible directional headroom
    delivered_kwh: np.ndarray  # grid-side, summed over the location's batteries; + sold, - bought
    fleet_soc: np.ndarray  # capacity-weighted SoC after each tick
    soc_final: np.ndarray  # per battery
    revenue_usd: np.ndarray  # per battery, at each tick's RT price
    sold_kwh: np.ndarray  # per battery, grid-side
    bought_kwh: np.ndarray  # per battery, grid-side
    active_ticks: int = 0  # ticks where energy moved
    seconds: float = 0.0
    soc_trace: np.ndarray | None = None  # (ticks, batteries), only with trace=True


def replay_location(
    ticks: Sequence[PriceTick],
    start: int,
    fleet: Fleet,
    strategy: Strategy | None,
    *,
    lag: int,
    history_ticks: int,
    dt_h: float,
    soc_min: float,
    soc_max: float,
    targets: np.ndarray | None = None,
    trace: bool = False,
) -> LocationRun:
    """Replay ticks[start:] for one location. Pass `targets` (one per replayed tick) instead
    of a strategy to use precomputed decisions (the bands fast path)."""
    import time

    if lag < 0:
        raise ValueError("lag must be >= 0")
    if targets is None and strategy is None:
        raise ValueError("need a strategy or precomputed targets")
    t0 = time.perf_counter()
    n_ticks = len(ticks) - start
    if targets is not None and len(targets) != n_ticks:
        raise ValueError(f"need {n_ticks} targets, got {len(targets)}")
    prices = [t.price_usd_mwh for t in ticks]
    cap, power, eff, avail = fleet.capacity_kwh, fleet.power_kw, fleet.efficiency, fleet.available
    soc = np.array(fleet.soc_init, dtype=np.float64)
    nb = fleet.size
    headroom = fleet_headroom_kw(power, avail)  # constant: availability never changes in a backtest
    fsoc = fleet_soc(soc, cap, avail)

    target_out = np.zeros(n_ticks)
    feasible_out = np.zeros(n_ticks)
    delivered_out = np.zeros(n_ticks)
    fsoc_out = np.empty(n_ticks)
    revenue = np.zeros(nb)
    sold = np.zeros(nb)
    bought = np.zeros(nb)
    soc_trace = np.empty((n_ticks, nb)) if trace else None
    active = 0

    for k in range(n_ticks):
        i = start + k
        if targets is not None:
            target = float(targets[k])
        else:
            seen = i - lag
            if seen >= 0:
                target = strategy.target_kw(
                    decision_tick(ticks[i], ticks[seen]), fsoc, headroom,
                    prices[max(0, seen - history_ticks):seen],
                )
            else:
                target = 0.0  # nothing published yet
        target_out[k] = target
        if target != 0.0:
            feasible, kwh, new_soc = deliver(target, soc, cap, power, eff, avail, dt_h, soc_min, soc_max)
            feasible_out[k] = feasible
            if kwh is not None:
                delivered_out[k] = float(kwh.sum())
                revenue += kwh * (prices[i] / 1000.0)
                if feasible > 0.0:
                    sold += kwh
                else:
                    bought -= kwh
                soc = new_soc
                fsoc = fleet_soc(soc, cap, avail)
                active += 1
        fsoc_out[k] = fsoc
        if soc_trace is not None:
            soc_trace[k] = soc

    return LocationRun(
        location=fleet.location, target_kw=target_out, feasible_kw=feasible_out, delivered_kwh=delivered_out,
        fleet_soc=fsoc_out, soc_final=soc, revenue_usd=revenue, sold_kwh=sold, bought_kwh=bought,
        active_ticks=active, seconds=time.perf_counter() - t0, soc_trace=soc_trace,
    )


def deliver(
    target: float,
    soc: np.ndarray,
    capacity_kwh: np.ndarray,
    power_kw: np.ndarray,
    efficiency: np.ndarray,
    available: np.ndarray,
    dt_h: float,
    soc_min: float,
    soc_max: float,
) -> tuple[float, np.ndarray | None, np.ndarray]:
    """Steps 2-4 of one tick for one location's batteries: (feasible_kw, grid kWh per battery, new SoC).

    headroom_kw, then allocate (clip the target to the feasible directional headroom, split it in
    proportion to each battery's headroom), then battery.step, metered from the SoC change with
    grid_kwh_from_soc_change. A hold, or a target with no feasible headroom, moves nothing: the
    kWh are None and the SoC array comes back unchanged (the exact shortcut of the module docstring).
    """
    if target == 0.0:
        return 0.0, None, soc
    discharge, charge = headroom_kw(soc, capacity_kwh, power_kw, efficiency, available, dt_h, soc_min, soc_max)
    feasible, setpoints = allocate(target, discharge, charge)
    if feasible == 0.0:
        return feasible, None, soc
    new_soc, _ = step(soc, capacity_kwh, power_kw, efficiency, available, setpoints, dt_h, soc_min, soc_max)
    return feasible, grid_kwh_from_soc_change(soc, new_soc, capacity_kwh, efficiency), new_soc


def fill_ledger(ledger: BacktestLedger, runs: Sequence[tuple[Sequence[PriceTick], np.ndarray]]) -> None:
    """Record every (tick, location) in tick order, locations in the given (config) order.

    That is the order a live coordinator records them in, so the per-day float sums match.
    """
    if not runs:
        return
    first = runs[0][0]
    if all(len(t) == len(first) and all(a.ts == b.ts for a, b in zip(t, first)) for t, _ in runs[1:]):
        for k in range(len(first)):
            for ticks, delivered in runs:
                ledger.record(ticks[k], float(delivered[k]))
        return
    keyed = sorted((t.ts, j, k) for j, (ticks, _) in enumerate(runs) for k, t in enumerate(ticks))
    for _, j, k in keyed:
        ticks, delivered = runs[j]
        ledger.record(ticks[k], float(delivered[k]))


# --- the bands fast path -------------------------------------------------------------------


def bands_windows(
    prices: np.ndarray, strategy: BandsStrategy, history_ticks: int, chunk: int = 2048
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(p_low, p_high, enough) for a decision whose published tick is index `seen`, for every seen.

    The seam passes history = prices[max(0, seen - history_ticks):seen]; the strategy holds
    while len(history) < min_samples and otherwise takes percentiles of the last
    window_ticks of it. np.percentile over a 2-D stack of those same windows gives the same
    order statistics and the same interpolation, so the values are bit-identical.
    Peak memory is one chunk of windows (2048 x 2016 floats, about 33 MB).
    """
    prices = np.asarray(prices, dtype=np.float64)
    n = len(prices)
    width = min(history_ticks, strategy.window_ticks)
    q = (strategy.low_percentile, strategy.high_percentile)
    enough = np.minimum(np.arange(n), history_ticks) >= strategy.min_samples
    p_low = np.full(n, np.nan)
    p_high = np.full(n, np.nan)
    for seen in range(min(width, n)):  # the window is still filling: prices[0:seen]
        if enough[seen]:
            p_low[seen], p_high[seen] = np.percentile(prices[:seen], q)
    if n > width and width > 0:
        windows = sliding_window_view(prices, width)  # windows[j] = prices[j:j + width]
        for a in range(width, n, chunk):
            b = min(a + chunk, n)
            lo_hi = np.percentile(windows[a - width:b - width], q, axis=1)
            p_low[a:b], p_high[a:b] = lo_hi[0], lo_hi[1]
    return p_low, p_high, enough


def bands_targets(
    prices: np.ndarray,
    start: int,
    lag: int,
    headroom_kw_value: float,
    windows: tuple[np.ndarray, np.ndarray, np.ndarray],
) -> np.ndarray:
    """BandsStrategy's decision for every replayed tick, from `bands_windows` output.

    Mirrors BandsStrategy.target_kw: hold unless H is finite and > 0, the published price
    is finite and the history has min_samples; charge (-H) if price <= p_low; else sell
    (+H) if price >= p_high and price > 0; else hold. Nothing published yet: hold.
    """
    prices = np.asarray(prices, dtype=np.float64)
    p_low, p_high, enough = windows
    seen = np.arange(start, len(prices)) - lag
    published = seen >= 0
    s = np.where(published, seen, 0)
    price = prices[s]
    h = float(headroom_kw_value)
    if not (math.isfinite(h) and h > 0.0):
        return np.zeros(len(seen))
    ok = published & enough[s] & np.isfinite(price)
    with np.errstate(invalid="ignore"):
        charge = ok & (price <= p_low[s])
        sell = ok & ~charge & (price >= p_high[s]) & (price > 0.0)
    return np.where(charge, -h, np.where(sell, h, 0.0))


def uses_bands_fast_path(strategy: Strategy) -> bool:
    """Only the exact v1 class: a subclass could change the rule."""
    return type(strategy) is BandsStrategy
