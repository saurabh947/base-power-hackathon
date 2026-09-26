"""The AI's prediction for the rest of a year: TimesFM, univariate, chained in chunks.

    python -m src.forecast.predict --year 2026 [--out db|parquet] [--locations LZ_HOUSTON ...]
                                   [--chunk-days 7] [--context N] [--limit-days N] [--model timesfm-3.0|timesfm-2.5]
                                   [--migrate] [--dry-run]

Per fleet city, from the year's real_until_utc (src.data.years: the end of the last RT interval that EVERY fleet
city has) to the end of the ERCOT year (Dec 31 24:00 Central):
  1. The context is the last `context_steps` real 15-min RT prices whose interval ENDED at or before the cutoff.
     Nothing after the cutoff is read, even where a city has more real data than the others (no look-ahead).
  2. The model forecasts the next chunk (7 days = 672 steps), univariate: p10/p50/p90.
  3. That chunk's p50 is appended to the context, and the next chunk is forecast from it, to the year's end.
All cities go through the model together, one batched call per chunk. The paths go to the price_predictions
table (--out db, the default when a database is configured) or to parquet under data/cache/predictions, stamped
with the model, the cutoff and issued_at. A re-run with the same cutoff replaces its rows.

Why univariate: the DA covariate the rolling job uses is only published a day ahead, never for the months to
come. The p50 is a median path: it is smooth and almost never spikes.

The band: fed its own smooth p50, the model grows very sure of itself (measured on the 2026 run: a mean
p10-p90 width of $36/MWh in the first week, $2-4 from the third week on). So from the second chunk on, each
point's band is at least as wide as the first week's at the same offset in the chunk (the same time of week for
7-day chunks): p10 = min(model p10, p50 - first-week gap), p90 likewise. The band then never claims more
certainty about December than about next week, though it still doesn't grow with the horizon.

Weights are handled as in src/forecast/model.py (pinned Hugging Face revisions, baked into the forecast image).
It runs in the forecast image as the Cloud Run Job fleet-sim-predict (8 vCPU, CPU only): a year's remainder
for 4 cities is ~60 model calls.
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping

import numpy as np
import pandas as pd

from src.forecast import inputs, predictions, store
from src.forecast.settings import ForecastSettings, load_settings

log = logging.getLogger("forecast.predict")
UTC = timezone.utc
STEP_MINUTES = predictions.STEP_MINUTES


@dataclass(frozen=True)
class PredictSettings:
    """config.yaml forecast.predict."""

    context_steps: int = 4096  # 42.7 days of 15-min history (TimesFM-3 takes up to 15,360)
    chunk_days: int = 7  # each chained forecast covers 7 days (672 steps)

    @property
    def chunk_steps(self) -> int:
        return self.chunk_days * 24 * 60 // STEP_MINUTES


def predict_settings(raw: Mapping | None = None) -> PredictSettings:
    if raw is None:
        from src.config import load_raw_config

        raw = (load_raw_config().get("forecast") or {}).get("predict") or {}
    s = PredictSettings(context_steps=int(raw.get("context_steps", 4096)), chunk_days=int(raw.get("chunk_days", 7)))
    if not (32 <= s.context_steps <= 15360) or s.chunk_days < 1:
        raise ValueError("forecast.predict: need 32 <= context_steps <= 15360 and chunk_days >= 1")
    return s


@dataclass(frozen=True)
class PredictPlan:
    year: int
    cutoff: pd.Timestamp  # UTC: real_until_utc, the first predicted interval starts here
    end: pd.Timestamp  # UTC: the end of the ERCOT year (or --limit-days)
    locations: tuple[str, ...]
    model: str
    context_steps: int
    chunk_steps: int
    out: str  # db | parquet

    @property
    def steps(self) -> int:
        return max(0, int((self.end - self.cutoff) / pd.Timedelta(minutes=STEP_MINUTES)))

    @property
    def chunks(self) -> int:
        return math.ceil(self.steps / self.chunk_steps) if self.steps else 0


def series_source() -> str:
    """The real prices' source for this job: the one src.data.series reads (Postgres when a database is configured,
    else the snapshot), so the cutoff and the context come from the same data."""
    from src.data import series

    return "postgres" if series.backend() == "postgres" else "snapshot"


def make_plan(year: int, settings: ForecastSettings, ps: PredictSettings, *, locations=None, out: str | None = None,
              cutoff: datetime | None = None, limit_days: int | None = None) -> PredictPlan:
    """The plan for `year`. cutoff defaults to the year's real_until_utc from the job's own data source."""
    from src.data import years

    locs = tuple(locations or settings.locations)
    if cutoff is None:
        y = years.info(year, locations=years.fleet_locations(), source=series_source(), refresh=True)
        if not y.available:
            raise SystemExit(f"{year} has no real prices for every fleet city yet: ingest it first")
        if y.real_until_utc is None:
            raise SystemExit(f"{year} is real for the whole year: there is nothing to predict")
        cutoff = y.real_until_utc
    cut = pd.Timestamp(cutoff).tz_convert("UTC")
    if cut.value % (STEP_MINUTES * 60 * 10**9):
        raise ValueError(f"cutoff {cut} is not on the {STEP_MINUTES}-minute grid")
    end = pd.Timestamp(years.year_window(year)[1])
    if limit_days:
        end = min(end, cut + pd.Timedelta(days=limit_days))
    out = out or ("db" if store.database_configured() else "parquet")
    return PredictPlan(year, cut, end, locs, settings.model, ps.context_steps, ps.chunk_steps, out)


Fetch = Callable[[str, datetime, datetime], pd.Series]


def load_contexts(plan: PredictPlan, fetch: Fetch | None = None) -> dict[str, np.ndarray]:
    """{location: the last context_steps real prices that ended at or before the cutoff}, float32, oldest first.
    Only [cutoff - context - 1 day, cutoff) is ever requested; a missing interval stays NaN."""
    if fetch is None:
        from src.data import series

        fetch = series.rt_prices
    lo = plan.cutoff - pd.Timedelta(minutes=STEP_MINUTES * plan.context_steps) - pd.Timedelta(days=1)
    out = {}
    for loc in plan.locations:
        raw = fetch(loc, lo.to_pydatetime(), plan.cutoff.to_pydatetime())
        raw = raw[pd.DatetimeIndex(raw.index).tz_convert("UTC") < plan.cutoff]  # belt and braces: never past it
        if raw.empty:
            raise RuntimeError(f"no real {loc} prices before {plan.cutoff}")
        grid = inputs.Grid.from_series(raw, STEP_MINUTES)
        ctx = grid.context(plan.cutoff, plan.context_steps)
        if grid.ended_before(plan.cutoff) < len(grid.values):
            raise AssertionError("context grid reaches past the cutoff")
        out[loc] = np.asarray(ctx, dtype=np.float32)
    return out


def run(plan: PredictPlan, *, model=None, fetch: Fetch | None = None, issued_at: datetime | None = None,
        settings: ForecastSettings | None = None) -> dict:
    """Make the paths (does not store them). Returns {"paths": {location: PredictionPath}, "contexts_fed": [...],
    "seconds", "model_seconds", "calls"}. contexts_fed[c][location] is what chunk c saw (for tests)."""
    t_start = time.perf_counter()
    contexts = load_contexts(plan, fetch)
    if model is None:
        from src.forecast.model import configure_torch, load_forecaster

        s = settings or load_settings()
        threads = configure_torch(s.threads)
        t0 = time.perf_counter()
        model = load_forecaster(plan.model, s.weights_dir, download=s.download, device=s.device,
                                batch_size=s.batch_size, max_context=plan.context_steps,
                                max_horizon=plan.chunk_steps)
        log.info("loaded %s (%s) in %.1f s on %d threads", plan.model, model.spec.licence, time.perf_counter() - t0,
                 threads)
    locs = list(plan.locations)
    q_parts = {loc: [] for loc in locs}
    chunk_ids = {loc: [] for loc in locs}
    gaps: dict[str, tuple[np.ndarray, np.ndarray]] = {}  # first chunk's (p50 - p10, p90 - p50) per step
    fed, done, model_s = [], 0, 0.0
    for c in range(plan.chunks):
        h = min(plan.chunk_steps, plan.steps - done)
        batch = [contexts[loc][-plan.context_steps:] for loc in locs]
        fed.append({loc: b.copy() for loc, b in zip(locs, batch)})
        t0 = time.perf_counter()
        q = np.asarray(model.forecast(batch, h, None), dtype=np.float64)  # (n, h, 3): p10, p50, p90
        model_s += time.perf_counter() - t0
        if q.shape != (len(locs), h, 3) or not np.isfinite(q).all():
            raise RuntimeError(f"chunk {c}: the model returned {q.shape} with non-finite values")
        q = np.sort(q, axis=-1)  # p10 <= p50 <= p90 even if a model crosses its quantiles
        for k, loc in enumerate(locs):
            if c == 0:
                gaps[loc] = (q[k, :, 1] - q[k, :, 0], q[k, :, 2] - q[k, :, 1])  # the first week's band, per step
            else:  # never narrower than the first week's band at the same offset (see the module docstring)
                below, above = gaps[loc]
                m = min(h, len(below))
                q[k, :m, 0] = np.minimum(q[k, :m, 0], q[k, :m, 1] - below[:m])
                q[k, :m, 2] = np.maximum(q[k, :m, 2], q[k, :m, 1] + above[:m])
            q_parts[loc].append(q[k])
            chunk_ids[loc].append(np.full(h, c, dtype=np.int16))
            contexts[loc] = np.concatenate([contexts[loc], q[k, :, 1].astype(np.float32)])[-plan.context_steps:]
        done += h
        log.info("chunk %d/%d: %d steps to %s, model %.1f s", c + 1, plan.chunks, h,
                 (plan.cutoff + pd.Timedelta(minutes=STEP_MINUTES * done)).isoformat(), model_s)
    issued = pd.Timestamp(issued_at or datetime.now(UTC)).tz_convert("UTC")
    ts = plan.cutoff.value + np.arange(plan.steps, dtype=np.int64) * (STEP_MINUTES * 60 * 10**9)
    paths = {}
    for loc in locs:
        q = np.concatenate(q_parts[loc]) if q_parts[loc] else np.zeros((0, 3))
        paths[loc] = predictions.PredictionPath(
            location=loc, model=plan.model, cutoff=plan.cutoff, issued_at=issued, ts=ts,
            p10=np.round(q[:, 0], 2), p50=np.round(q[:, 1], 2), p90=np.round(q[:, 2], 2),
            chunk=np.concatenate(chunk_ids[loc]) if chunk_ids[loc] else np.zeros(0, np.int16))
    return {"paths": paths, "contexts_fed": fed, "calls": plan.chunks, "seconds": round(time.perf_counter() - t_start, 2),
            "model_seconds": round(model_s, 2)}


def summary(plan: PredictPlan, paths: Mapping[str, predictions.PredictionPath]) -> dict:
    out = {"year": plan.year, "cutoff": plan.cutoff.isoformat(), "end": plan.end.isoformat(), "steps": plan.steps,
           "chunks": plan.chunks, "model": plan.model, "out": plan.out, "locations": {}}
    for loc, p in paths.items():
        out["locations"][loc] = {"p50_mean": round(float(np.mean(p.p50)), 2), "p50_min": round(float(np.min(p.p50)), 2),
                                 "p50_max": round(float(np.max(p.p50)), 2),
                                 "band_mean": round(float(np.mean(p.p90 - p.p10)), 2)}
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m src.forecast.predict", description=__doc__.split("\n")[0])
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--out", choices=("db", "parquet"), help="default: db if a database is configured")
    ap.add_argument("--locations", nargs="+", help="default: forecast.locations (the fleet's cities)")
    ap.add_argument("--chunk-days", type=int, help="days per chained forecast (config forecast.predict.chunk_days)")
    ap.add_argument("--context", type=int, help="context steps (config forecast.predict.context_steps)")
    ap.add_argument("--limit-days", type=int, help="DEV: predict only this many days after the cutoff")
    ap.add_argument("--model", choices=("timesfm-3.0", "timesfm-2.5"))
    ap.add_argument("--cache-dir", help="parquet output folder (config forecast.predict.cache_dir)")
    ap.add_argument("--migrate", action="store_true", help="apply pending migrations first (--out db)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, then exit (no model)")
    return ap


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    args = build_parser().parse_args(argv)
    settings = load_settings()
    if args.model:
        settings = settings.with_overrides(model=args.model)
    ps = predict_settings()
    if args.chunk_days:
        ps = PredictSettings(ps.context_steps, args.chunk_days)
    if args.context:
        ps = PredictSettings(args.context, ps.chunk_days)
    plan = make_plan(args.year, settings, ps, locations=args.locations, out=args.out, limit_days=args.limit_days)
    log.info("plan: %s from %s to %s, %d steps in %d chunks of %d, context %d, cities %s, out=%s", plan.model,
             plan.cutoff.isoformat(), plan.end.isoformat(), plan.steps, plan.chunks, plan.chunk_steps,
             plan.context_steps, ", ".join(plan.locations), plan.out)
    if args.dry_run or plan.steps == 0:
        return 0
    if args.migrate and plan.out == "db":
        from src import db

        log.info("migrations applied: %s", ", ".join(db.migrate()) or "nothing new")
    result = run(plan, settings=settings)
    n = predictions.write(result["paths"].values(), out=plan.out, cache_dir=args.cache_dir)
    log.info("stored %d points (%s); %s", n, plan.out, summary(plan, result["paths"]))
    log.info("done in %.1f s (model %.1f s, %d calls)", result["seconds"], result["model_seconds"], result["calls"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

