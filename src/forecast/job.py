"""Rolling-origin forecasts for a year: RT price per location, and ERCOT demand.

    python -m src.forecast.job --year 2025 [--locations LZ_HOUSTON LZ_NORTH] [--limit-days N]
                               [--out db|parquet] [--series rt_price demand] [--start YYYY-MM-DD]
                               [--model timesfm-3.0|timesfm-2.5] [--context N] [--every-minutes N]
                               [--covariates none|da] [--cache-dir DIR] [--no-resume] [--dry-run]

Every `every_minutes` (config: hourly) from local midnight on Jan 1 of --year
to the next Jan 1, it issues one forecast per location and series:
  rt_price  15-min steps, next 24 h (96 points), p10/p50/p90, $/MWh
  demand    hourly steps, next 48 h (48 points), p10/p50/p90, MW (location 'ERCOT')
A forecast issued at T sees only intervals that ended at or before T
(src/forecast/inputs.py). Output goes to the forecasts table (--out db, the
default when DATABASE_URL or DB_HOST is set) or to parquet under
data/cache/forecasts. Chunks of days are written as they finish, and a
re-run skips issue times already stored, so a timed-out job just resumes.

On Cloud Run with --tasks N, each task takes a contiguous share of the days
(CLOUD_RUN_TASK_INDEX / CLOUD_RUN_TASK_COUNT).
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.forecast import inputs, store
from src.forecast.settings import ForecastSettings, SeriesSettings, load_settings

log = logging.getLogger("forecast.job")

CHUNK_DAYS = 4  # days per write; 4 days x 24 issues x 2 locations = 6 full batches of 32


@dataclass(frozen=True)
class Plan:
    settings: ForecastSettings
    series: tuple[str, ...]
    locations: tuple[str, ...]
    start: pd.Timestamp  # UTC, first issue time (inclusive)
    end: pd.Timestamp  # UTC, exclusive
    out: str  # db | parquet

    def issue_times(self, s: SeriesSettings) -> pd.DatetimeIndex:
        return inputs.issue_times(self.start, self.end, s.every_minutes)

    def targets(self) -> list[tuple[str, str]]:
        """(series, location) pairs this plan forecasts."""
        out = []
        for name in self.series:
            if name == "rt_price":
                out += [("rt_price", loc) for loc in self.locations]
            else:
                out.append(("demand", self.settings.demand_location))
        return out


def local_midnight(d: date, tz: str) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(d, datetime.min.time(), tzinfo=ZoneInfo(tz))).tz_convert("UTC")


def make_plan(args: argparse.Namespace, settings: ForecastSettings) -> Plan:
    tz = settings.timezone
    start_day = date.fromisoformat(args.start) if args.start else date(args.year, 1, 1)
    end_day = date(args.year + 1, 1, 1)
    if args.limit_days:
        end_day = min(end_day, start_day + timedelta(days=args.limit_days))
    start, end = local_midnight(start_day, tz), local_midnight(end_day, tz)
    task_index = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    task_count = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    if task_count > 1:  # contiguous share of whole days per task
        days = (end_day - start_day).days
        lo = start_day + timedelta(days=days * task_index // task_count)
        hi = start_day + timedelta(days=days * (task_index + 1) // task_count)
        start, end = local_midnight(lo, tz), local_midnight(hi, tz)
    out = args.out or ("db" if store.database_configured() else "parquet")
    return Plan(settings, tuple(args.series), tuple(args.locations or settings.locations), start, end, out)


def apply_overrides(settings: ForecastSettings, args: argparse.Namespace) -> ForecastSettings:
    rt, demand = settings.rt_price, settings.demand
    if args.context:
        rt = replace(rt, context_steps=args.context)
    if args.every_minutes:  # both series; e.g. 1740 (29 h) samples every hour of the day across a year
        for s in (rt, demand):
            if args.every_minutes % s.step_minutes:
                raise SystemExit(f"--every-minutes must be a multiple of {s.name}'s {s.step_minutes}-minute step")
        rt = replace(rt, every_minutes=args.every_minutes)
        demand = replace(demand, every_minutes=args.every_minutes)
    if args.covariates:
        rt = replace(rt, covariates=args.covariates)
    if args.demand_context:
        demand = replace(demand, context_steps=args.demand_context,
                         min_context_steps=min(demand.min_context_steps, args.demand_context))
    changes = {"rt_price": rt, "demand": demand}
    if args.model:
        changes["model"] = args.model
    if args.cache_dir:
        changes["cache_dir"] = os.path.abspath(args.cache_dir)
    if args.batch_size:
        changes["batch_size"] = args.batch_size
    if args.threads:
        changes["threads"] = args.threads
    return settings.with_overrides(**changes)


# --- data --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Inputs:
    grids: dict[tuple[str, str], inputs.Grid]  # (series, location) -> grid
    da: dict[str, np.ndarray]  # location -> DA price on the RT grid (covariates == 'da')


def load_inputs(plan: Plan) -> Inputs:
    """Real ERCOT series (src.data.series: Postgres or the committed snapshot) covering every context.

    RT prices are required. Demand is optional: if its series cannot be read
    (e.g. load not ingested yet) it is skipped with an error in the log, so the
    price forecasts the strategy needs still get made.
    """
    from src.data import series as data

    s = plan.settings
    grids, da = {}, {}
    for name, location in plan.targets():
        cfg = s.series(name)
        lead = timedelta(minutes=cfg.step_minutes * cfg.context_steps) + timedelta(days=1)
        lo, hi = plan.start - lead, plan.end + timedelta(days=3)
        try:
            raw = data.rt_prices(location, lo, hi) if name == "rt_price" else data.system_load(lo, hi)
            if raw.empty:
                raise RuntimeError(f"no {name} data for {location} in [{lo}, {hi})")
            grid = inputs.Grid.from_series(raw, cfg.step_minutes)
            if name == "rt_price" and cfg.covariates == "da":
                da[location] = inputs.da_on_grid(data.da_prices(location, lo, hi), grid)
        except Exception as exc:
            if name == "rt_price":
                raise
            log.error("skipping %s/%s: cannot read its input series (%s)", name, location, exc)
            continue
        grids[(name, location)] = grid
    return Inputs(grids, da)


# --- run -----------------------------------------------------------------------------------


def run(plan: Plan, *, resume: bool = True, model=None) -> dict:
    """Forecast every (series, location, issue time) in the plan. Returns counts and timings."""
    s = plan.settings
    data = load_inputs(plan)
    sink = store.DbSink() if plan.out == "db" else store.ParquetSink(s.cache_dir)
    todo: dict[tuple[str, str], list[pd.Timestamp]] = {}
    skipped_existing = skipped_short = 0
    for name, location in plan.targets():
        cfg = s.series(name)
        grid = data.grids.get((name, location))
        if grid is None:
            continue
        done = sink.existing(s.model, name, location, plan.start, plan.end) if resume else set()
        keep = []
        for t in plan.issue_times(cfg):
            if t in done:
                skipped_existing += 1
            elif np.isfinite(grid.context(t, cfg.context_steps)).sum() < cfg.min_context_steps:
                skipped_short += 1
            else:
                keep.append(t)
        todo[(name, location)] = keep
    total = sum(len(v) for v in todo.values())
    log.info("plan: %s, %s .. %s, %d forecasts to make (%d already stored, %d with too little history), out=%s",
             s.model, plan.start.isoformat(), plan.end.isoformat(), total, skipped_existing, skipped_short, plan.out)
    if total == 0:
        return {"forecasts": 0, "skipped_existing": skipped_existing, "skipped_short": skipped_short, "seconds": 0.0}

    if model is None:
        from src.forecast.model import configure_torch, load_forecaster

        threads = configure_torch(s.threads)
        t0 = time.perf_counter()
        horizons = [s.series(n).horizon_steps for n in plan.series]
        contexts = [s.series(n).context_steps for n in plan.series]
        model = load_forecaster(s.model, s.weights_dir, download=s.download, device=s.device,
                                batch_size=s.batch_size, max_context=max(contexts), max_horizon=max(horizons))
        log.info("loaded %s (%s) in %.1f s on %d threads", s.model, model.spec.licence, time.perf_counter() - t0,
                 threads)
    if s.rt_price.covariates == "da" and "rt_price" in plan.series and not model.supports_covariates:
        # timesfm-2.5 covariates need the XReg/JAX path; FORECAST_MODEL=timesfm-2.5 alone should still work.
        log.warning("%s takes no covariates here: rt_price is forecast univariate", s.model)
        s = s.with_overrides(rt_price=replace(s.rt_price, covariates="none"))

    chunk = pd.Timedelta(days=chunk_days(plan))
    made, t_start = 0, time.perf_counter()
    lo = plan.start
    while lo < plan.end:
        hi = min(lo + chunk, plan.end)
        rows = []
        for name in plan.series:  # one model call per series, all locations together: fuller batches
            tasks = [(loc, t) for (n, loc), times in todo.items() if n == name for t in times if lo <= t < hi]
            if tasks:
                rows += forecast_rows(model, s, s.series(name), tasks, data)
        if rows:
            sink.write(rows)
            made += len(rows)
            elapsed = time.perf_counter() - t_start
            rate = made / elapsed
            log.info("%s: %d/%d forecasts, %.2f/s, elapsed %.1f min, ETA %.1f min", hi.date(), made, total, rate,
                     elapsed / 60, (total - made) / rate / 60)
        lo = hi
    seconds = time.perf_counter() - t_start
    if isinstance(sink, store.DbSink):
        sink.close()
    return {"forecasts": made, "skipped_existing": skipped_existing, "skipped_short": skipped_short,
            "seconds": round(seconds, 1), "per_second": round(made / seconds, 2) if seconds else None}


def chunk_days(plan: Plan) -> int:
    """Days per write: at least CHUNK_DAYS, and enough issue times for ~4 full batches per series."""
    s = plan.settings
    per_day = max(len(plan.locations) * 1440 / s.rt_price.every_minutes if "rt_price" in plan.series else 0,
                  1440 / s.demand.every_minutes if "demand" in plan.series else 0)
    return max(CHUNK_DAYS, math.ceil(4 * s.batch_size / per_day))


def forecast_rows(model, s: ForecastSettings, cfg: SeriesSettings,
                  tasks: list[tuple[str, pd.Timestamp]], data: Inputs) -> list[store.ForecastRow]:
    """Forecast (location, issue time) pairs of one series in one batched model call."""
    grids = [data.grids[(cfg.name, loc)] for loc, _ in tasks]
    contexts = [g.context(t, cfg.context_steps) for g, (_, t) in zip(grids, tasks)]
    covariates = None
    if cfg.name == "rt_price" and cfg.covariates == "da":
        covariates = [inputs.da_covariate(data.da[loc], g, t, cfg.context_steps, cfg.horizon_steps,
                                          s.da_publish_local, s.timezone) for g, (loc, t) in zip(grids, tasks)]
    q = model.forecast(contexts, cfg.horizon_steps, covariates)
    return [
        store.ForecastRow(t, loc, cfg.name, s.model, g.first_open(t), cfg.step_minutes,
                          q[k, :, 0], q[k, :, 1], q[k, :, 2])
        for k, (g, (loc, t)) in enumerate(zip(grids, tasks))
    ]


def dry_run(plan: Plan) -> dict:
    """Counts and projected compute, without loading the model."""
    from src.forecast.bench import gflop_per_token, patch_tokens
    from src.forecast.model import spec_for

    spec = spec_for(plan.settings.model)
    counts, tokens = {}, 0
    for name, location in plan.targets():
        cfg = plan.settings.series(name)
        n = len(plan.issue_times(cfg))
        counts[f"{name}/{location}"] = n
        variates = 2 if (name == "rt_price" and cfg.covariates == "da" and spec.family == "timesfm3") else 1
        tokens += n * patch_tokens(cfg.context_steps, cfg.horizon_steps, variates, spec.family)
    tflop = tokens * gflop_per_token(spec.params_millions) / 1000
    return {"forecasts": counts, "tokens": tokens, "tflop": round(tflop, 1),
            "minutes_at_150_gflops": round(tflop * 1000 / 150 / 60, 1),
            "minutes_at_300_gflops": round(tflop * 1000 / 300 / 60, 1)}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m src.forecast.job", description=__doc__.split("\n")[0])
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--locations", nargs="+")
    ap.add_argument("--series", nargs="+", choices=("rt_price", "demand"), default=["rt_price", "demand"])
    ap.add_argument("--start", help="first local day (YYYY-MM-DD); default Jan 1 of --year")
    ap.add_argument("--limit-days", type=int, help="stop after N days")
    ap.add_argument("--out", choices=("db", "parquet"), help="default: db if a database is configured")
    ap.add_argument("--model", choices=("timesfm-3.0", "timesfm-2.5"))
    ap.add_argument("--context", type=int, help="rt_price context steps (config: rt_price.context_steps)")
    ap.add_argument("--demand-context", type=int, help="demand context steps (config: demand.context_steps)")
    ap.add_argument("--every-minutes", type=int, help="rt_price issue cadence")
    ap.add_argument("--covariates", choices=("none", "da"))
    ap.add_argument("--cache-dir", help="parquet output folder (config: forecast.cache_dir)")
    ap.add_argument("--batch-size", type=int)
    ap.add_argument("--threads", type=int)
    ap.add_argument("--no-resume", action="store_true", help="recompute issue times already stored")
    ap.add_argument("--migrate", action="store_true", help="apply pending migrations first (--out db)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and projected compute, then exit")
    return ap


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    args = build_parser().parse_args(argv)
    settings = apply_overrides(load_settings(), args)
    plan = make_plan(args, settings)
    if args.dry_run:
        print(dry_run(plan))
        return 0
    if args.migrate and plan.out == "db":
        from src import db

        log.info("migrations applied: %s", ", ".join(db.migrate()) or "nothing new")
    result = run(plan, resume=not args.no_resume)
    log.info("done: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
