"""Full-year backtest (spec milestone M4): in-process, vectorized replay of the whole fleet.

    python -m src.backtest.runner --strategies bands,peak --year 2025 [--pitch] [--no-db]

Each strategy replays the year through src/backtest/engine.py, which runs the live path's
semantics per tick and per location: the Strategy seam through decision_tick(now,
published) at lag sim.decision_lag_ticks, symmetric headroom, a clip to the feasible
directional headroom, proportional allocation, src.sim.battery physics, and the Ledger per
(tick, location) at that tick's RT price. The fleet is src.sim.fleet.generate_batteries
(20,000 seeded batteries), split between the configured locations.

Every strategy runs twice: at the configured lag (3 ticks, the headline) and at lag 0, the
spec-literal variant, which is stored in the record's metrics["lag0"]. The headline replay also
yields metrics["insights"] (src/backtest/insights.py: surplus power soaked up, energy sold in price
spikes, trade volume and prices), read off the same replay. The perfect-hindsight
ceiling comes from src/backtest/ceiling.py. Records go to data/results/ as JSON and, when a
database is configured, to backtest_runs; --pitch marks them as the Results screen's set.

A strategy that is not in this build (for example 'forecast' before it lands) or that fails
is skipped: its record says why, and so do the Results.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np

from src.backtest import engine, insights
from src.backtest.engine import (  # re-exported: the parity tests of the worker fleet import these
    Fleet,
    LocationRun,
    TickSeries,
    bands_targets,
    bands_windows,
    build_fleets,
    fill_ledger,
    load_ticks,
    replay_location,
    uses_bands_fast_path,
    year_bounds,
)
from src.backtest.metrics import compact, fleet_totals, replay_metrics
from src.config import REPO_ROOT, Config, load_config, load_raw_config
from src.core.interfaces import PriceFeed, Strategy
from src.ledger.backtest import BacktestLedger
from src.sim.allocation import fleet_headroom_kw

__all__ = [
    "Fleet", "LocationRun", "TickSeries", "Replay", "BacktestOutcome", "backtest_settings", "bands_targets",
    "bands_windows", "build_fleets", "fill_ledger", "load_ticks", "make_named_strategy", "replay",
    "replay_location", "run_backtest", "run_strategy", "year_bounds", "main",
]

UTC = timezone.utc
HINDSIGHT = "hindsight"  # backtest_runs.strategy of the perfect-hindsight ceiling row

DEFAULTS: dict = {
    "year": 2025,
    "strategies": ["bands", "forecast", "peak"],
    "headline": ["forecast", "bands"],
    "lag0_variant": True,
    "warmup_days": 7,
    "results_dir": "data/results",
    "ceiling": {"ratio_step": 0.05, "cycles_per_day_cap": 1.0, "workers": 4},
    "labels": {
        "bands": "Simple rule",
        "forecast": "AI forecast (TimesFM-3)",
        "peak": "Evening peak",
        HINDSIGHT: "Best possible (perfect hindsight)",
    },
}


def backtest_settings(raw: Mapping | None = None) -> dict:
    """config.yaml's `backtest:` section over DEFAULTS (one level of nesting is merged)."""
    section = dict((raw if raw is not None else load_raw_config()).get("backtest") or {})
    out = json.loads(json.dumps(DEFAULTS))
    for key, value in section.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


class StrategyUnavailable(RuntimeError):
    """The strategy is not in this build or cannot be constructed."""

    plain: str | None = None  # the Results' reason, when there is a better one than "not available yet"


class ForecastsMissing(StrategyUnavailable):
    """The forecast strategy's cached forecasts don't cover the year in every city."""

    def __init__(self, message: str, locations: Sequence[str], year: int) -> None:
        super().__init__(message)
        self.locations = list(locations)
        from src.sim import geo

        self.plain = (f"its AI price forecasts for {geo.join_names([geo.city_name(loc) for loc in self.locations])} "
                      f"aren't ready for {year} yet.")


def make_named_strategy(name: str, cfg: Config, location: str | None = None) -> Strategy:
    """make_strategy(cfg.strategy, tick_seconds, name): plugs in 'forecast' once it is built.

    With `location`, a forecast instance loads only that city's forecasts: it never reads another city's
    (target_kw looks up tick.location), so the decisions are the same and the database is read once per city.
    """
    from src.strategy import make_strategy

    context = {"locations": [location]} if name == "forecast" and location is not None else {}
    try:
        return make_strategy(cfg.strategy, cfg.sim.tick_seconds, name, **context)
    except Exception as exc:  # unknown name, missing module, missing forecasts...
        raise StrategyUnavailable(f"{type(exc).__name__}: {exc}") from exc


def forecast_coverage(strategy: Strategy, location: str, ticks: list) -> float | None:
    """Share of `ticks` that have a usable forecast (issued at or before the tick, not too old).

    None for strategies that don't read forecasts. A forecast strategy with no
    forecasts would silently hold all year, so run_strategy refuses to run it.
    """
    books = getattr(strategy, "books", None)
    if books is None:
        return None
    book = books.get(location)
    if book is None or len(book) == 0 or not ticks:
        return 0.0
    ts_ns = np.fromiter((int(t.ts.timestamp()) * 10**9 for t in ticks), dtype=np.int64, count=len(ticks))
    idx = np.searchsorted(book.issued, ts_ns, side="right") - 1
    found = idx >= 0
    age = ts_ns - book.issued[np.clip(idx, 0, None)]
    return float(np.mean(found & (age <= strategy.max_age_ns)))


def history_ticks(cfg: Config) -> int:
    """How much price history the caller hands the strategy: what the M0 skeleton passes."""
    return cfg.strategy.bands.window_days * cfg.sim.ticks_per_day


# --- one replay: one strategy, one decision lag, every location ------------------------------


@dataclass
class Replay:
    strategy: str
    strategy_repr: str
    lag: int
    runs: dict[str, LocationRun]
    summary: dict  # Ledger.summary()
    metrics: dict
    fast_path: bool
    seconds: float


def replay(
    name: str,
    cfg: Config,
    series: Mapping[str, TickSeries],
    fleets: Mapping[str, Fleet],
    *,
    lag: int,
    fast: bool = True,
    expected_ticks: int | None = None,
    window_cache: dict | None = None,
    strategies: Mapping[str, Strategy] | None = None,
    trace: bool = False,
) -> Replay:
    """Replay every location with its own Strategy instance, then fill one Ledger.

    `strategies` maps location -> instance (built with make_named_strategy when omitted);
    strategies are pure, so one instance per location serves every lag.
    `window_cache` shares the bands percentile windows between lags (they depend only on prices).
    `trace` keeps every battery's SoC after every tick (small fleets only: tests).
    """
    t0 = time.perf_counter()
    b = cfg.battery
    hist = history_ticks(cfg)
    runs: dict[str, LocationRun] = {}
    used_fast, reprs = False, set()
    for loc, fleet in fleets.items():
        ts = series[loc]
        # one Strategy instance per location (RUNLOG "Per-location objects")
        strategy = strategies[loc] if strategies is not None else make_named_strategy(name, cfg, loc)
        reprs.add(repr(strategy))
        targets = None
        if fast and uses_bands_fast_path(strategy):
            prices = ts.prices
            key = (loc, id(ts), len(ts.ticks), hist, strategy.low_percentile, strategy.high_percentile,
                   strategy.window_ticks, strategy.min_samples)
            if window_cache is None or key not in window_cache:
                windows = bands_windows(prices, strategy, hist)
                if window_cache is not None:
                    window_cache[key] = windows
            else:
                windows = window_cache[key]
            targets = bands_targets(prices, ts.start, lag, fleet_headroom_kw(fleet.power_kw, fleet.available),
                                    windows)
            used_fast = True
        runs[loc] = replay_location(
            ts.ticks, ts.start, fleet, strategy, lag=lag, history_ticks=hist, dt_h=cfg.sim.dt_hours,
            soc_min=b.soc_min, soc_max=b.soc_max, targets=targets, trace=trace,
        )
    ledger = BacktestLedger({loc: f.size for loc, f in fleets.items()})
    fill_ledger(ledger, [(series[loc].replayed, runs[loc].delivered_kwh) for loc in fleets])
    summary = ledger.summary()
    expected = expected_ticks if expected_ticks is not None else max(len(s.replayed) for s in series.values())
    metrics = replay_metrics(summary, runs, fleets, series, soc_min=b.soc_min, soc_max=b.soc_max,
                             expected_ticks=expected)
    return Replay(name, " / ".join(sorted(reprs)), lag, runs, summary, metrics, used_fast, time.perf_counter() - t0)


# --- records ---------------------------------------------------------------------------------


def _jsonable(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, dtime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    return value


def utc_iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True,
                             timeout=2)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


@dataclass
class Context:
    """Everything the strategies of one backtest share: config, prices, fleet."""

    cfg: Config
    settings: dict
    year: int
    series: dict[str, TickSeries]
    fleets: dict[str, Fleet]
    prices_backend: str
    start: datetime
    end: datetime
    expected_ticks: int
    window_cache: dict = field(default_factory=dict)

    def params(self, **extra) -> dict:
        cfg, b = self.cfg, self.cfg.battery
        totals = fleet_totals(self.fleets, b.soc_min, b.soc_max)
        return _jsonable({
            "year": self.year,
            **extra,
            "fleet": {
                "size": totals["size"],
                "locations": list(self.fleets),  # config order (jsonb does not keep object key order)
                "by_location": totals["by_location"],
                "power_kw_total": totals["power_kw"],
                "capacity_kwh_total": totals["capacity_kwh"],
                "seed": cfg.seed,
                "assignment": "src.sim.fleet.generate_batteries, contiguous blocks per location in config order",
                "battery_ranges": {
                    "power_kw": list(b.power_kw),
                    "capacity_kwh": list(b.capacity_kwh),
                    "cap_power_at_capacity": b.cap_power_at_capacity,
                    "efficiency": b.efficiency,
                    "soc_bounds": [b.soc_min, b.soc_max],
                    "soc_init": b.soc_init,
                },
            },
            "prices": {
                "source": "ERCOT RT settlement point prices, native 15-min, forward-filled to 5-min ticks",
                "backend": self.prices_backend,
                "replay_start_utc": utc_iso(self.start),
                "replay_end_utc": utc_iso(self.end),
                "ticks": {loc: len(s.replayed) for loc, s in self.series.items()},
                "warmup_ticks": {loc: s.start for loc, s in self.series.items()},
            },
            "config": {**dataclasses.asdict(cfg), "backtest": self.settings},
            "code": {"git": _git_commit()},
        })


def build_context(
    year: int,
    cfg: Config | None = None,
    settings: dict | None = None,
    *,
    fleet_size: int | None = None,
    feed: PriceFeed | None = None,
) -> Context:
    """Load the year's real prices (plus the warm-up days before it) and build the fleet."""
    cfg = cfg or load_config()
    settings = settings or backtest_settings()
    start, end = year_bounds(year)
    warmup_start = start - timedelta(days=int(settings["warmup_days"]))
    locations = list(cfg.fleet.locations)
    backend = "caller-supplied PriceFeed"
    if feed is None:
        feed, backend = engine.feed_from_series(locations, warmup_start, end)
    series = load_ticks(feed, locations, start, end, warmup_start)
    missing = [loc for loc, s in series.items() if not s.replayed]
    if missing:
        raise NoPriceData(f"There are no real ERCOT prices for {year} ({', '.join(missing)}) to replay.")
    fleets = build_fleets(fleet_size or cfg.fleet.size, cfg.fleet.locations, cfg.battery, cfg.seed)
    expected = int((end - start).total_seconds()) // cfg.sim.tick_seconds
    return Context(cfg, settings, year, series, fleets, backend, start, end, expected)


class NoPriceData(RuntimeError):
    pass


def run_strategy(name: str, ctx: Context, *, fast: bool = True, lag0: bool | None = None,
                 strategies: Mapping[str, Strategy] | None = None) -> dict:
    """Replay one strategy at the headline lag (and lag 0), and return its record.

    Raises StrategyUnavailable if the strategy cannot be built (e.g. 'forecast' before it lands).
    """
    started = datetime.now(UTC)
    if strategies is None:
        strategies = {loc: make_named_strategy(name, ctx.cfg, loc) for loc in ctx.fleets}
    need = float(ctx.settings.get("forecast_min_coverage", 0.99))
    short = {}  # every city must be covered: one headline number is one decision method for the whole fleet
    for loc, strategy in strategies.items():
        coverage = forecast_coverage(strategy, loc, ctx.series[loc].replayed)
        if coverage is not None and coverage < need:
            short[loc] = coverage
    if short:
        where = ", ".join(f"{cov:.1%} at {loc}" for loc, cov in short.items())
        raise ForecastsMissing(f"forecasts cover {where} of {ctx.year} (need {need:.0%} in every city); "
                               "run the forecast job first (make cloud-forecast)", list(short), ctx.year)
    lag = ctx.cfg.sim.decision_lag_ticks
    main_run = replay(name, ctx.cfg, ctx.series, ctx.fleets, lag=lag, fast=fast, expected_ticks=ctx.expected_ticks,
                      window_cache=ctx.window_cache, strategies=strategies)
    metrics = dict(main_run.metrics)
    metrics["lag_ticks"] = lag
    metrics["replay_seconds"] = round(main_run.seconds, 3)
    # what the AI adds (GET /results/ai-gains), read off this same replay: no extra replay
    metrics["insights"] = insights.from_replay(main_run, ctx.series)
    want_lag0 = ctx.settings["lag0_variant"] if lag0 is None else lag0
    if want_lag0 and lag != 0:
        zero = replay(name, ctx.cfg, ctx.series, ctx.fleets, lag=0, fast=fast, expected_ticks=ctx.expected_ticks,
                      window_cache=ctx.window_cache, strategies=strategies)
        metrics["lag0"] = {**compact(zero.metrics), "lag_ticks": 0, "replay_seconds": round(zero.seconds, 3)}
    finished = datetime.now(UTC)
    s = main_run.summary
    label = ctx.settings["labels"].get(name, name)
    per_battery = metrics["per_battery_usd_year"]
    return {
        "run_id": str(uuid.uuid4()),
        "strategy": name,
        "label": label,
        "year": ctx.year,
        "started_at": utc_iso(started),
        "finished_at": utc_iso(finished),
        "fleet_total_usd": float(s["total_usd"]),
        "per_battery_avg_usd": float(s["per_battery_avg_usd"]),
        "per_location": {k: float(v) for k, v in s["per_location_usd"].items()},
        "top_days": metrics["top_days"],
        "metrics": metrics,
        "params": ctx.params(
            strategy=name, strategy_repr=main_run.strategy_repr, lag_ticks=lag, lag0_variant=bool(want_lag0),
            history_ticks=history_ticks(ctx.cfg),
            decisions=("bands percentiles precomputed for all ticks (bit-identical to the seam)"
                       if main_run.fast_path else "Strategy.target_kw called every tick"),
        ),
        "notes": (f"{label}: ${per_battery:,.2f} per battery, ${s['total_usd']:,.0f} for the fleet, "
                  f"{ctx.year}, decision lag {lag} ticks."),
    }


def skipped_record(name: str, ctx: Context, reason: str, *, unavailable: bool, plain: str | None = None) -> dict:
    """A marker row: the strategy was asked for but could not run. The Results say so."""
    now = utc_iso(datetime.now(UTC))
    plain = plain or ("it isn't available in this version of the simulator yet." if unavailable
                      else "it stopped with an error during the full-year run.")
    return {
        "run_id": str(uuid.uuid4()), "strategy": name, "label": ctx.settings["labels"].get(name, name),
        "year": ctx.year, "started_at": now, "finished_at": now, "fleet_total_usd": None,
        "per_battery_avg_usd": None, "per_location": None, "top_days": None,
        "metrics": {"skipped": True, "reason": reason, "reason_plain": plain},
        "params": ctx.params(strategy=name), "notes": f"Skipped: {reason}",
    }


@dataclass
class BacktestOutcome:
    records: list[dict]
    skipped: dict[str, str]
    ceiling: dict | None
    stored_in_db: bool
    json_paths: list[str]

    @property
    def run_ids(self) -> list[str]:
        return [r["run_id"] for r in self.records if not r["metrics"].get("skipped")]


def run_backtest(
    strategies: Sequence[str],
    year: int,
    *,
    cfg: Config | None = None,
    settings: dict | None = None,
    pitch: bool = False,
    use_db: bool | None = None,
    ceiling: bool = True,
    fleet_size: int | None = None,
    feed: PriceFeed | None = None,
    fast: bool = True,
    out_dir: str | Path | None = None,
    log: Callable[[str], None] = print,
    save: bool = True,
) -> BacktestOutcome:
    """Run strategies over a year, add the ceiling, export JSON, and persist to Postgres.

    save=False stores nothing: the caller gets the records and decides (the API's cancellable jobs).
    """
    from src.backtest import store

    settings = settings or backtest_settings()
    t0 = time.perf_counter()
    ctx = build_context(year, cfg, settings, fleet_size=fleet_size, feed=feed)
    log(f"loaded {year}: {', '.join(f'{loc} {len(s.replayed):,} ticks' for loc, s in ctx.series.items())} "
        f"({ctx.prices_backend}); fleet {sum(f.size for f in ctx.fleets.values()):,} batteries "
        f"in {time.perf_counter() - t0:.1f}s")
    records, skipped = [], {}
    for name in strategies:
        t1 = time.perf_counter()
        try:
            record = run_strategy(name, ctx, fast=fast)
        except Exception as exc:  # a missing or failing strategy must not sink the others
            unavailable = isinstance(exc, StrategyUnavailable)
            reason = str(exc) if unavailable else f"{type(exc).__name__}: {exc}"
            if not unavailable:
                log(traceback.format_exc())
            skipped[name] = reason
            records.append(skipped_record(name, ctx, reason, unavailable=unavailable,
                                          plain=getattr(exc, "plain", None)))
            log(f"{name}: skipped ({reason})")
            continue
        records.append(record)
        m = record["metrics"]
        log(f"{name}: ${m['per_battery_usd_year']:,.2f}/battery, fleet ${record['fleet_total_usd']:,.0f} "
            f"in {time.perf_counter() - t1:.1f}s")
    ceiling_record = None
    if ceiling:
        from src.backtest import ceiling as ceiling_mod

        t1 = time.perf_counter()
        ceiling_record = ceiling_mod.ceiling_record(ctx, out_dir=out_dir, log=log)
        records.append(ceiling_record)
        log(f"ceiling: ${ceiling_record['per_battery_avg_usd']:,.2f}/battery in {time.perf_counter() - t1:.1f}s")
    if not save:
        return BacktestOutcome(records, skipped, ceiling_record, False, [])
    saved = store.save_records(records, pitch=pitch, use_db=use_db, out_dir=out_dir, settings=settings, cfg=ctx.cfg)
    return BacktestOutcome(records, skipped, ceiling_record, saved.in_db, saved.paths)


# --- CLI -------------------------------------------------------------------------------------


def format_report(outcome: BacktestOutcome) -> str:
    lines = []
    for r in outcome.records:
        m = r["metrics"]
        if m.get("skipped"):
            lines.append(f"\n{r['label']} ({r['strategy']}): SKIPPED: {m['reason']}")
            continue
        if r["strategy"] == HINDSIGHT:
            continue
        lines += [
            f"\n{r['label']} ({r['strategy']}), {r['year']}, decision lag {m['lag_ticks']} ticks, run {r['run_id']}",
            f"  per battery per year  ${m['per_battery_usd_year']:>12,.2f}",
            f"  fleet per year        ${m['fleet_usd_year']:>12,.0f}",
            *[f"    {loc:<18}  ${v['usd']:>12,.0f}  (${v['per_battery_usd']:,.2f}/battery, "
              f"{v['batteries']:,} batteries)" for loc, v in m["per_location"].items()],
            f"  $/kW-year {m['usd_per_kw_year']:.2f}   $/kWh-year {m['usd_per_kwh_year']:.2f}   "
            f"cycles/year {m['cycles_per_year']:.1f}",
            f"  sold {m['sold_kwh'] / 1000:,.0f} MWh, bought {m['bought_kwh'] / 1000:,.0f} MWh "
            f"(avg ${m['avg_sell_usd_mwh'] or 0:,.1f} vs ${m['avg_buy_usd_mwh'] or 0:,.1f} per MWh)",
            f"  top days: " + ", ".join(f"{d['date_local']} ${d['usd']:,.0f}" for d in m["top_days"][:5]),
            f"  replay {m['replay_seconds']:.1f}s, coverage {m['coverage']['fraction']:.4f}",
        ]
        ins = m.get("insights")
        if ins:
            level = ins["spike_charge_level"]
            lines.append(f"  surplus bought at <= $0: {ins['surplus_mwh']:,.1f} MWh; sold in spikes >= "
                         f"${ins['spike_usd_mwh']:,.0f}/MWh: {ins['spike_mwh']:,.1f} MWh over {ins['spike_events']} "
                         f"city spikes (charge level when they began {'n/a' if level is None else f'{level:.1%}'})")
        if "lag0" in m:
            z = m["lag0"]
            lines.append(f"  lag 0 (spec-literal): ${z['per_battery_usd_year']:,.2f}/battery, "
                         f"fleet ${z['fleet_usd_year']:,.0f} ({z['replay_seconds']:.1f}s)")
    if outcome.ceiling:
        m = outcome.ceiling["metrics"]
        u, c = m["uncapped"], m["capped"]
        lines += [
            f"\n{outcome.ceiling['label']}: ${u['per_battery_usd_year']:,.2f} per battery "
            f"(fleet ${u['fleet_usd_year']:,.0f}, {u['cycles_per_year']:.0f} cycles/yr)",
            f"  capped at {m['cycles_per_day_cap']:g} cycle/day: ${c['per_battery_usd_year']:,.2f} per battery "
            f"(fleet ${c['fleet_usd_year']:,.0f})",
            f"  method: {m['method']}",
        ]
    if outcome.stored_in_db or outcome.json_paths:
        lines.append(f"\nstored: {'backtest_runs + ' if outcome.stored_in_db else ''}{', '.join(outcome.json_paths)}")
    else:
        lines.append("\nstored: nothing (the records were handed to the caller)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    settings = backtest_settings()
    p = argparse.ArgumentParser(prog="python -m src.backtest.runner", description=__doc__.split("\n")[0])
    p.add_argument("--strategies", default=",".join(settings["strategies"]),
                   help="comma-separated (default: config backtest.strategies)")
    p.add_argument("--year", type=int, default=int(settings["year"]))
    p.add_argument("--pitch", action="store_true", help="mark these runs as the Results screen's set")
    p.add_argument("--no-db", action="store_true", help="JSON export only, even if a database is configured")
    p.add_argument("--no-ceiling", action="store_true", help="skip the perfect-hindsight ceiling")
    p.add_argument("--no-lag0", action="store_true", help="skip the spec-literal lag-0 variant")
    p.add_argument("--seam", action="store_true", help="call every strategy per tick (no bands fast path)")
    p.add_argument("--fleet-size", type=int, help="override fleet.size (e.g. a quick 200-battery run)")
    p.add_argument("--out", help=f"JSON export directory (default: {settings['results_dir']})")
    p.add_argument("--status-file", help="write {run_ids, skipped, error} JSON here when done (used by the API)")
    p.add_argument("--records-out", help="store nothing: write the finished records here as JSON instead (the API's "
                                         "cancellable jobs store them once the run is complete)")
    args = p.parse_args(argv)
    if args.no_lag0:
        settings["lag0_variant"] = False
    names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    status: dict = {"run_ids": [], "skipped": {}, "error": None}
    try:
        outcome = run_backtest(names, args.year, settings=settings, pitch=args.pitch,
                               use_db=False if args.no_db else None, ceiling=not args.no_ceiling,
                               fleet_size=args.fleet_size, fast=not args.seam, out_dir=args.out,
                               log=lambda s: print(s, file=sys.stderr, flush=True), save=not args.records_out)
        if args.records_out:
            from src.backtest.store import clean

            tmp = Path(args.records_out + ".tmp")
            tmp.write_text(json.dumps(clean(outcome.records), allow_nan=False))
            tmp.replace(args.records_out)  # whole or not at all
        status.update(run_ids=outcome.run_ids, skipped=outcome.skipped,
                      ceiling_run_id=outcome.ceiling["run_id"] if outcome.ceiling else None)
        print(format_report(outcome))
        return 0
    except NoPriceData as exc:
        status["error"] = str(exc)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        status["error"] = f"The full-year run failed ({type(exc).__name__}: {exc})."
        traceback.print_exc()
        return 1
    finally:
        if args.status_file:
            Path(args.status_file).write_text(json.dumps(status))


if __name__ == "__main__":
    sys.exit(main())
