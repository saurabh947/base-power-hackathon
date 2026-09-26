"""CPU throughput benchmark for the forecast job.

    python -m src.forecast.bench [--model timesfm-3.0] [--contexts 1024 2048] [--batch 32] [--n 64]

Runs the model on real LZ_HOUSTON contexts from data/snapshot and prints
ms per forecast, patch tokens per second and achieved GFLOP/s. Tokens are
(context + horizon patches) x variates, so the numbers carry over to any plan.
It then projects the full-year job (2 locations + ERCOT demand) from the
configured plan. Keep --n small on a laptop: each forecast is ~10-50 GFLOP.
"""
from __future__ import annotations

import argparse
import math
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from src.forecast.settings import SeriesSettings, load_settings

UTC = timezone.utc


def patch_tokens(context: int, horizon: int, variates: int = 1, family: str = "timesfm3") -> int:
    """Transformer tokens one forecast costs (TimesFM: 32-point patches)."""
    ctx = math.ceil(context / 32)
    if family == "timesfm3":  # stitching: forecast patches + (output/input patch ratio) - 1
        hor = max(math.ceil((horizon - 32) / 32), 1) + 1
    else:  # 2.5: one 128-point output patch per decode step, flip-invariance doubles the work
        return 2 * ctx * variates
    return (ctx + hor) * variates


def gflop_per_token(params_millions: int) -> float:
    return 2 * params_millions / 1000.0


def issues_per_year(s: SeriesSettings) -> int:
    return 365 * 24 * 60 // s.every_minutes


def plan_tokens(settings, family: str) -> dict[str, int]:
    """Tokens for a full year: every location's RT price plus ERCOT demand."""
    rt, dm = settings.rt_price, settings.demand
    variates = 2 if rt.covariates == "da" else 1
    return {
        "rt_price": issues_per_year(rt) * len(settings.locations)
        * patch_tokens(rt.context_steps, rt.horizon_steps, variates, family),
        "demand": issues_per_year(dm) * patch_tokens(dm.context_steps, dm.horizon_steps, 1, family),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.forecast.bench", description=__doc__.split("\n")[0])
    ap.add_argument("--model")
    ap.add_argument("--contexts", type=int, nargs="+", default=[1024])
    ap.add_argument("--horizon", type=int, default=96)
    ap.add_argument("--batch", type=int, nargs="+", default=[32])
    ap.add_argument("--n", type=int, default=64, help="forecasts per measurement")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--covariate", action="store_true", help="add the DA price as a known-future covariate")
    args = ap.parse_args(argv)

    from src.data import series
    from src.forecast.model import configure_torch, load_forecaster, spec_for

    settings = load_settings()
    name = args.model or settings.model
    spec = spec_for(name)
    threads = configure_torch(args.threads or settings.threads)
    t0 = time.perf_counter()
    model = load_forecaster(name, settings.weights_dir, download=settings.download, device=settings.device,
                            batch_size=max(args.batch), max_context=max(args.contexts), max_horizon=args.horizon)
    print(f"{name}: loaded in {time.perf_counter() - t0:.1f} s, {threads} torch threads")

    end = datetime(2025, 5, 16, 18, tzinfo=UTC)
    need = max(args.contexts) + args.n
    prices = series.rt_prices("LZ_HOUSTON", end - timedelta(minutes=15 * need), end).to_numpy(np.float32)
    da = series.da_prices("LZ_HOUSTON", end - timedelta(minutes=15 * need), end + timedelta(days=2))
    da_q = np.repeat(da.to_numpy(np.float32), 4)  # hourly -> 15-min

    per_token = gflop_per_token(spec.params_millions)
    best = None
    for context in args.contexts:
        for batch in args.batch:
            if spec.family == "timesfm3":  # 2.5 fixes its batch size when it compiles
                model._model.config.per_core_batch_size = batch
            ctxs = [prices[i:i + context] for i in range(args.n)]
            covs = [da_q[i:i + context + args.horizon][None, :] for i in range(args.n)] if args.covariate else None
            model.forecast(ctxs[:batch], args.horizon, None if covs is None else covs[:batch])  # warm-up
            t = time.perf_counter()
            model.forecast(ctxs, args.horizon, covs)
            dt = time.perf_counter() - t
            tokens = patch_tokens(context, args.horizon, 2 if args.covariate else 1, spec.family) * args.n
            gflops = tokens * per_token / dt
            print(f"context {context:5d} batch {batch:3d}: {1000 * dt / args.n:7.1f} ms/forecast, "
                  f"{tokens / dt:8.0f} tokens/s, {gflops:6.0f} GFLOP/s")
            best = max(best or 0.0, tokens / dt)

    plan = plan_tokens(settings, spec.family)
    total = sum(plan.values())
    print(f"\nconfigured plan ({settings.rt_price.context_steps} ctx, every {settings.rt_price.every_minutes} min, "
          f"covariates {settings.rt_price.covariates}): {total / 1e6:.2f} M tokens = "
          f"{total * per_token / 1e3:.0f} TFLOP per year")
    for name_, tok in plan.items():
        print(f"  {name_:9s} {tok / 1e6:6.2f} M tokens")
    print(f"  here: {total / best / 60:.1f} min at {best:.0f} tokens/s")
    for gf in (150, 300):
        print(f"  at {gf} GFLOP/s (8-vCPU x86 guess): {total * per_token / gf / 60:.0f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
