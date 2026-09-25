"""Best possible (perfect hindsight): a per-day LP ceiling for the whole fleet.

The model is the one the strategies run under (src/sim/battery.py): grid-side power
|P| <= power_kw, storage gains |P|*sqrt(eta) per hour when charging and loses P/sqrt(eta)
when discharging, SoC within [soc_min, soc_max]. The planner knows every RT price of the
operating day (America/Chicago) in advance, plans each day on its own, and must end the day
at the charge level it started at (it picks that level).

Per unit of capacity, with ratio r = power_kw / capacity_kwh, one day is the LP
    maximize   sum_k p_k/1000 * dt * (d_k - c_k)             ($ per kWh of capacity)
    subject to e_{k+1} = e_k + dt * (sqrt(eta) c_k - d_k / sqrt(eta)),   e_n = e_0
               soc_min <= e_k <= soc_max,  c_k, d_k >= 0,  c_k + d_k <= r
               capped variant only: sum_k dt * d_k / sqrt(eta) <= cap * (soc_max - soc_min)
solved with HiGHS (scipy.optimize.linprog) on the native 15-minute settlement intervals
(dt = 0.25 h). A price is constant over its interval's three 5-minute ticks, so at a
positive price any LP schedule is exactly what a constant setpoint does in the 5-minute
model. At a negative price the LP may both charge and discharge within one interval (the
c + d <= r relaxation); that only makes it an upper bound, never an underestimate.

Scaling makes the whole fleet exact: every constraint scales with capacity, so a battery's
best revenue is capacity * F(r). F is solved on a grid of ratios spanning the fleet; it is
concave and nondecreasing in r (an LP's optimum is concave in its right-hand side), so
linear interpolation between grid points is a lower bound and extending neighbouring chords
gives an upper bound. The fleet number is the interpolation; `bracket` holds both bounds.

The headline is the uncapped LP. The simulated batteries have no cycle limit or wear cost,
and neither do the strategies (the simple rule cycles more than once on some days), so only
the uncapped LP bounds what any strategy could earn under the same rules. The one-cycle-a-day
number is shown as a reference for warranty-limited operation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
from scipy import sparse
from scipy.optimize import linprog

UTC = timezone.utc
INTERVAL_MINUTES = 15
METHOD_VERSION = 1  # bump when the LP changes, so cached curves are recomputed


@dataclass(frozen=True)
class DayLP:
    revenue_per_kwh: float  # $ per kWh of capacity for the day
    discharged_per_kwh: float  # kWh discharged out of storage per kWh of capacity
    charge: np.ndarray | None = None  # grid-side kW per kWh of capacity, per interval
    discharge: np.ndarray | None = None
    level: np.ndarray | None = None  # SoC at each interval boundary (n + 1 values)


_MATRICES: dict[tuple[int, float, float], tuple] = {}


def _matrices(n: int, dt: float, root_eta: float):
    key = (n, dt, root_eta)
    if key not in _MATRICES:
        eye = sparse.identity(n, format="csr")
        zeros_col = sparse.csr_matrix((n, 1))
        # rows k < n: e_{k+1} - e_k - dt*sqrt(eta)*c_k + dt/sqrt(eta)*d_k = 0; row n: e_n - e_0 = 0
        de = sparse.hstack([zeros_col, eye]) - sparse.hstack([eye, zeros_col])
        cyclic = sparse.csr_matrix(([-1.0, 1.0], ([0, 0], [0, n])), shape=(1, n + 1))
        a_eq = sparse.vstack([
            sparse.hstack([-dt * root_eta * eye, (dt / root_eta) * eye, de]),
            sparse.hstack([sparse.csr_matrix((1, 2 * n)), cyclic]),
        ]).tocsr()
        a_ub = sparse.hstack([eye, eye, sparse.csr_matrix((n, n + 1))]).tocsr()  # c_k + d_k <= r
        cycle_row = sparse.hstack([sparse.csr_matrix((1, n)), sparse.csr_matrix(np.full((1, n), dt / root_eta)),
                                   sparse.csr_matrix((1, n + 1))]).tocsr()
        _MATRICES[key] = (a_eq, a_ub, sparse.vstack([a_ub, cycle_row]).tocsr())
    return _MATRICES[key]


def solve_day(
    prices_usd_mwh: Sequence[float],
    ratio: float,
    *,
    efficiency: float,
    soc_min: float,
    soc_max: float,
    interval_h: float = INTERVAL_MINUTES / 60,
    tradable: Sequence[bool] | None = None,
    cycles_cap: float | None = None,
    schedule: bool = False,
) -> DayLP:
    """The day's best schedule per kWh of capacity. Intervals with tradable=False (no price) stay idle."""
    p = np.asarray(prices_usd_mwh, dtype=np.float64)
    n = len(p)
    if n == 0:
        return DayLP(0.0, 0.0)
    if not (ratio >= 0 and math.isfinite(ratio)):
        raise ValueError(f"ratio must be finite and >= 0, got {ratio!r}")
    ok = np.ones(n, dtype=bool) if tradable is None else np.asarray(tradable, dtype=bool)
    p = np.where(ok, p, 0.0)
    if not np.all(np.isfinite(p)):
        raise ValueError("tradable prices must be finite")
    if not ok.any() and not schedule:
        return DayLP(0.0, 0.0)  # no price all day: nothing can be traded
    root_eta = math.sqrt(efficiency)
    a_eq, a_ub, a_ub_cap = _matrices(n, interval_h, root_eta)
    cost = np.concatenate([p / 1000.0 * interval_h, -p / 1000.0 * interval_h, np.zeros(n + 1)])
    power_hi = np.where(ok, ratio, 0.0)
    bounds = np.column_stack([
        np.concatenate([np.zeros(2 * n), np.full(n + 1, soc_min)]),
        np.concatenate([power_hi, power_hi, np.full(n + 1, soc_max)]),
    ])
    if cycles_cap is None:
        a, b = a_ub, np.full(n, ratio)
    else:
        a, b = a_ub_cap, np.append(np.full(n, ratio), cycles_cap * (soc_max - soc_min))
    res = linprog(cost, A_ub=a, b_ub=b, A_eq=a_eq, b_eq=np.zeros(n + 1), bounds=bounds, method="highs")
    if res.status != 0:
        raise RuntimeError(f"hindsight LP failed: {res.message}")
    x = res.x
    c, d, e = x[:n], x[n:2 * n], x[2 * n:]
    out_kwh = float(d.sum() * interval_h / root_eta)
    if schedule:
        return DayLP(-float(res.fun), out_kwh, c.copy(), d.copy(), e.copy())
    return DayLP(-float(res.fun), out_kwh)


# --- prices by operating day ------------------------------------------------------------------


@dataclass(frozen=True)
class Day:
    date_local: str
    prices: np.ndarray  # $/MWh per 15-min interval (0 where missing)
    tradable: np.ndarray  # False where the interval has no price


def days_from_ticks(ticks, start: datetime, end: datetime, tz: str = "America/Chicago") -> list[Day]:
    """Group a location's replayed 5-minute ticks into 15-minute intervals per operating day.

    Uses the very ticks the strategies replay, so the ceiling sees the same prices.
    """
    zone = ZoneInfo(tz)
    step = INTERVAL_MINUTES * 60
    t0 = start.timestamp()
    n = int((end - start).total_seconds()) // step
    prices = np.full(n, np.nan)
    for t in ticks:
        k = int((t.ts.timestamp() - t0) // step)
        if 0 <= k < n and math.isnan(prices[k]):
            prices[k] = t.price_usd_mwh
    days = []
    day = start.astimezone(zone).date()
    k = 0
    while k < n:
        nxt = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(days=1)
        nxt_utc = datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone).astimezone(UTC)
        k_end = min(n, int((nxt_utc.timestamp() - t0) // step))
        chunk = prices[k:k_end]
        ok = np.isfinite(chunk)
        days.append(Day(day.isoformat(), np.where(ok, chunk, 0.0), ok))
        k, day = k_end, nxt.date()
    return days


# --- per-kWh curves over the ratio grid -------------------------------------------------------


def ratio_grid(ratios: np.ndarray, step: float) -> np.ndarray:
    """Grid points every `step`, covering [min(ratios), max(ratios)]."""
    lo = math.floor(float(np.min(ratios)) / step + 1e-9) * step
    hi = math.ceil(float(np.max(ratios)) / step - 1e-9) * step
    grid = np.round(np.arange(lo, hi + step / 2, step), 10)
    return grid if len(grid) >= 2 else np.array([lo, lo + step])


def _curve_point(args) -> tuple[str, float, float, float, float, float]:
    loc, ratio, days, physics, cap = args
    u_rev = u_out = c_rev = c_out = 0.0
    for prices, ok in days:
        u = solve_day(prices, ratio, tradable=ok, **physics)
        c = solve_day(prices, ratio, tradable=ok, cycles_cap=cap, **physics)
        u_rev += u.revenue_per_kwh
        u_out += u.discharged_per_kwh
        c_rev += c.revenue_per_kwh
        c_out += c.discharged_per_kwh
    return loc, ratio, u_rev, u_out, c_rev, c_out


def curves(
    days_by_location: Mapping[str, Sequence[Day]],
    grid: np.ndarray,
    *,
    efficiency: float,
    soc_min: float,
    soc_max: float,
    cycles_cap: float,
    workers: int = 1,
) -> dict:
    """{location: {"ratio", "uncapped_usd", "uncapped_out", "capped_usd", "capped_out"}}, per kWh of capacity."""
    physics = {"efficiency": efficiency, "soc_min": soc_min, "soc_max": soc_max}
    tasks = [(loc, float(r), [(d.prices, d.tradable) for d in days], physics, cycles_cap)
             for loc, days in days_by_location.items() for r in grid]
    if workers > 1 and len(tasks) > 1:
        import multiprocessing

        with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            points = list(pool.map(_curve_point, tasks))
    else:
        points = [_curve_point(t) for t in tasks]
    out = {loc: {"ratio": [float(r) for r in grid], "uncapped_usd": [], "uncapped_out": [], "capped_usd": [],
                 "capped_out": []} for loc in days_by_location}
    for loc, _, u_rev, u_out, c_rev, c_out in points:  # pool.map keeps the task order
        o = out[loc]
        o["uncapped_usd"].append(u_rev)
        o["uncapped_out"].append(u_out)
        o["capped_usd"].append(c_rev)
        o["capped_out"].append(c_out)
    return out


def interpolate_bounds(grid: np.ndarray, values: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(interpolated, lower, upper) for a concave nondecreasing curve known at grid points.

    Linear interpolation is a lower bound. Upper bound: the next grid value, and the chords of
    the neighbouring intervals extended over x (a concave curve lies below them there).
    """
    g, v = np.asarray(grid, dtype=np.float64), np.asarray(values, dtype=np.float64)
    x = np.asarray(x, dtype=np.float64)
    mid = np.interp(x, g, v)
    j = np.clip(np.searchsorted(g, x, side="right") - 1, 0, len(g) - 2)  # x in [g_j, g_{j+1}]
    upper = v[j + 1].copy()
    slopes = np.diff(v) / np.diff(g)
    left = j - 1 >= 0
    upper[left] = np.minimum(upper[left], v[j[left]] + slopes[j[left] - 1] * (x[left] - g[j[left]]))
    right = j + 1 < len(slopes)
    upper[right] = np.minimum(upper[right], v[j[right] + 1] - slopes[j[right] + 1] * (g[j[right] + 1] - x[right]))
    at_grid = np.isclose(x, g[j]) | np.isclose(x, g[j + 1])
    upper = np.where(at_grid, mid, np.maximum(upper, mid))
    return mid, mid.copy(), upper


def fleet_value(fleets, curve: Mapping[str, dict], variant: str, soc_min: float, soc_max: float) -> dict:
    """Fleet totals for one variant ("uncapped" or "capped") from the per-kWh curves."""
    per_location, low, high, storage_out, usable, n = {}, 0.0, 0.0, 0.0, 0.0, 0
    for loc, fleet in fleets.items():
        c = curve[loc]
        grid = np.asarray(c["ratio"])
        ratios = fleet.power_kw / fleet.capacity_kwh
        rev, lo, hi = interpolate_bounds(grid, np.asarray(c[f"{variant}_usd"]), ratios)
        out = np.interp(ratios, grid, np.asarray(c[f"{variant}_out"]))
        per_location[loc] = float((fleet.capacity_kwh * rev).sum())
        low += float((fleet.capacity_kwh * lo).sum())
        high += float((fleet.capacity_kwh * hi).sum())
        storage_out += float((fleet.capacity_kwh * out).sum())
        usable += float(fleet.capacity_kwh.sum()) * (soc_max - soc_min)
        n += fleet.size
    total = sum(per_location.values())
    return {
        "per_battery_usd_year": total / n if n else 0.0,
        "fleet_usd_year": total,
        "per_location_usd": per_location,
        "cycles_per_year": storage_out / usable if usable else 0.0,
        "bracket_fleet_usd": {"low": low, "high": high},
        "bracket_relative": (high - low) / total if total else 0.0,
    }


# --- the record ------------------------------------------------------------------------------


def cache_key(days_by_location: Mapping[str, Sequence[Day]], grid, physics: dict, cap: float) -> str:
    h = hashlib.sha256()
    h.update(json.dumps({"v": METHOD_VERSION, "grid": [float(g) for g in grid], "physics": physics, "cap": cap,
                         "interval_minutes": INTERVAL_MINUTES}, sort_keys=True).encode())
    for loc, days in sorted(days_by_location.items()):
        h.update(loc.encode())
        for d in days:
            h.update(d.date_local.encode())
            h.update(np.ascontiguousarray(d.prices).tobytes())
            h.update(np.ascontiguousarray(d.tradable).tobytes())
    return h.hexdigest()[:20]


def ceiling_record(ctx, *, out_dir: str | Path | None = None, workers: int | None = None,
                   log: Callable[[str], None] = print) -> dict:
    """The ceiling as a backtest_runs record (strategy 'hindsight'); per-kWh curves are cached on disk."""
    import uuid

    from src.backtest.runner import HINDSIGHT, utc_iso
    from src.backtest.store import find_ceiling_curves, results_dir

    t0 = time.perf_counter()
    started = datetime.now(UTC)
    b, settings = ctx.cfg.battery, ctx.settings
    step = float(settings["ceiling"]["ratio_step"])
    cap = float(settings["ceiling"]["cycles_per_day_cap"])
    workers = int(settings["ceiling"].get("workers", 1)) if workers is None else workers
    workers = max(1, min(workers, os.cpu_count() or 1))
    physics = {"efficiency": b.efficiency, "soc_min": b.soc_min, "soc_max": b.soc_max}
    effs = {float(e) for f in ctx.fleets.values() for e in np.unique(f.efficiency)}
    if effs != {b.efficiency}:
        raise ValueError(f"the ceiling assumes one efficiency for the fleet, got {sorted(effs)}")
    days = {loc: days_from_ticks(ctx.series[loc].replayed, ctx.start, ctx.end) for loc in ctx.fleets}
    all_ratios = np.concatenate([f.power_kw / f.capacity_kwh for f in ctx.fleets.values()])
    grid = ratio_grid(all_ratios, step)
    key = cache_key(days, grid, physics, cap)
    base = results_dir(out_dir, settings)
    cache = base / "cache" / f"ceiling_curves_{key}.json"
    curve = json.loads(cache.read_text()) if cache.exists() else None
    if curve is not None:
        log(f"ceiling: per-kWh curves from cache {cache.name}")
    else:
        curve = find_ceiling_curves(key, ctx.year, base)
        if curve is not None:
            log("ceiling: per-kWh curves from an earlier ceiling run with the same prices and physics")
        else:
            log(f"ceiling: solving {len(grid)} ratios x {sum(len(d) for d in days.values())} location-days x 2 "
                f"with {workers} process(es)...")
            curve = curves(days, grid, cycles_cap=cap, workers=workers, **physics)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(curve))
    uncapped = fleet_value(ctx.fleets, curve, "uncapped", b.soc_min, b.soc_max)
    capped = fleet_value(ctx.fleets, curve, "capped", b.soc_min, b.soc_max)
    negative = {loc: int(sum(int(((d.prices < 0) & d.tradable).sum()) for d in ds)) for loc, ds in days.items()}
    n = sum(f.size for f in ctx.fleets.values())
    method = (f"Per-day LP (HiGHS) with perfect knowledge of each day's real-time prices at "
              f"{INTERVAL_MINUTES}-minute settlement resolution, each day ending at the charge level it started at. "
              f"Exact for all {n:,} batteries by scaling: revenue = capacity x F(power/capacity), F solved at "
              f"{len(grid)} power-to-capacity ratios from {grid[0]:g} to {grid[-1]:g} and interpolated "
              f"(interpolation bounds within {uncapped['bracket_relative'] * 100:.3f}%).")
    finished = datetime.now(UTC)
    label = settings["labels"].get(HINDSIGHT, "Best possible (perfect hindsight)")
    return {
        "run_id": str(uuid.uuid4()),
        "strategy": HINDSIGHT,
        "label": label,
        "year": ctx.year,
        "started_at": utc_iso(started),
        "finished_at": utc_iso(finished),
        "fleet_total_usd": uncapped["fleet_usd_year"],
        "per_battery_avg_usd": uncapped["per_battery_usd_year"],
        "per_location": uncapped["per_location_usd"],
        "top_days": None,
        "metrics": {
            "headline": "uncapped",
            "per_battery_usd_year": uncapped["per_battery_usd_year"],
            "fleet_usd_year": uncapped["fleet_usd_year"],
            "uncapped": uncapped,
            "capped": capped,
            "cycles_per_day_cap": cap,
            "why_uncapped": ("The simulated batteries have no cycle limit or wear cost, and neither do the "
                             "strategies, so only the uncapped plan bounds what any strategy could earn under "
                             "the same rules. The one-cycle-a-day figure is a reference for warranty-limited "
                             "operation."),
            "method": method,
            "scope": "all batteries (exact per-kWh scaling), not a sample",
            "interval_minutes": INTERVAL_MINUTES,
            "ratio_grid": [float(g) for g in grid],
            "curves": curve,
            "days": {loc: len(ds) for loc, ds in days.items()},
            "negative_price_intervals": negative,
            "cache_key": key,
            "seconds": round(time.perf_counter() - t0, 3),
        },
        "params": ctx.params(strategy=HINDSIGHT, physics=physics, ratio_step=step, cycles_per_day_cap=cap,
                             method_version=METHOD_VERSION),
        "notes": (f"{label}: ${uncapped['per_battery_usd_year']:,.2f} per battery "
                  f"(${capped['per_battery_usd_year']:,.2f} at {cap:g} cycle/day), {ctx.year}."),
    }
