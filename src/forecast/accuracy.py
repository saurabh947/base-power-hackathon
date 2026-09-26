"""Forecast accuracy against seasonal-naive baselines, on real ERCOT actuals.

    python -m src.forecast.accuracy [--cache-dir DIR] [--model M] [--series rt_price demand]

For every stored forecast (issued at T, points t_i) and every point with a
real actual y(t_i):
  - TimesFM: MAE and median absolute error of p50, pinball loss averaged over
    q = 0.1, 0.5, 0.9, and how often y falls inside [p10, p90] (ideal 80%).
  - SN1, seasonal naive, "same time yesterday": y(t_i - 24 h * k), the most
    recent same-time-of-day value known at T (k = 1 within 24 h; 2 for
    hours 24-48 of the demand horizon). It is a point forecast, so its pinball
    loss is 0.5 x its MAE.
  - SN7: the p10/p50/p90 of the same time of day over the 7 most recent known
    days. A fairer probabilistic baseline.
Skill = 1 - model / baseline (positive: TimesFM is better).
Baselines use only intervals that ended at or before T, like the model.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src.forecast.inputs import Grid
from src.forecast.provider import ForecastBook

QS = np.array([0.1, 0.5, 0.9])


def pinball(y: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Mean pinball loss over QS. y: (...,), q: (..., 3) -> (...)."""
    e = y[..., None] - q
    return np.maximum(QS * e, (QS - 1.0) * e).mean(axis=-1)


@dataclass(frozen=True)
class Scored:
    """Aligned arrays over every (forecast, point) with a known actual."""

    y: np.ndarray  # actual
    model: np.ndarray  # (n, 3)
    sn1: np.ndarray  # (n,)
    sn7: np.ndarray  # (n, 3)
    lead_steps: np.ndarray  # point index within its forecast


def score(book: ForecastBook, actual: Grid) -> Scored:
    """Line every forecast point up with its actual and the two baselines."""
    if book.step_minutes != actual.step_minutes:
        raise ValueError("book and actuals must share a grid")
    step_ns = book.step_minutes * 60 * 10**9
    per_day = 24 * 60 // book.step_minutes
    t0 = actual.t0.as_unit("ns").value
    ys, ms, s1, s7, leads = [], [], [], [], []
    for i in range(len(book)):
        idx = (book.point_times(i) - t0) // step_ns  # grid index of each point
        known = (int(book.issued[i]) - t0) // step_ns  # intervals [0, known) ended by T
        ok = (idx >= 0) & (idx < len(actual.values))
        idx_ok = idx[ok]
        y = actual.values[idx_ok]
        # SN1: the most recent same-time-of-day index that ended by T
        back = np.ceil((idx_ok - known + 1) / per_day).clip(min=1).astype(np.int64)
        sn1 = _take(actual.values, idx_ok - back * per_day)
        # SN7: that day and the 6 before it
        lags = (back[:, None] + np.arange(7)[None, :]) * per_day
        sn7 = np.nanquantile(_take(actual.values, idx_ok[:, None] - lags), QS, axis=1).T
        ys.append(y)
        ms.append(np.stack([book.p10[i][ok], book.p50[i][ok], book.p90[i][ok]], axis=-1))
        s1.append(sn1)
        s7.append(sn7)
        leads.append(np.arange(book.horizon)[ok])
    y = np.concatenate(ys)
    keep = np.isfinite(y) & np.isfinite(np.concatenate(s1))
    return Scored(y[keep], np.concatenate(ms)[keep], np.concatenate(s1)[keep], np.concatenate(s7)[keep],
                  np.concatenate(leads)[keep])


def _take(values: np.ndarray, idx: np.ndarray) -> np.ndarray:
    out = np.full(idx.shape, np.nan, dtype=np.float64)
    ok = (idx >= 0) & (idx < len(values))
    out[ok] = values[idx[ok]]
    return out


def metrics(s: Scored, mask: np.ndarray | None = None) -> dict:
    m = slice(None) if mask is None else mask
    y, q, sn1, sn7 = s.y[m], s.model[m], s.sn1[m], s.sn7[m]
    mae = float(np.abs(y - q[:, 1]).mean())
    mae1 = float(np.abs(y - sn1).mean())
    mae7 = float(np.abs(y - sn7[:, 1]).mean())
    pin = float(pinball(y, q).mean())
    pin1 = 0.5 * mae1
    pin7 = float(pinball(y, sn7).mean())
    return {
        "points": int(len(y)),
        "mae": mae, "medae": float(np.median(np.abs(y - q[:, 1]))),
        "pinball": pin, "coverage_80": float(((y >= q[:, 0]) & (y <= q[:, 2])).mean()),
        "sn1_mae": mae1, "sn1_pinball": pin1, "sn7_mae": mae7, "sn7_pinball": pin7,
        "skill_mae_vs_sn1": 1 - mae / mae1, "skill_pinball_vs_sn1": 1 - pin / pin1,
        "skill_pinball_vs_sn7": 1 - pin / pin7,
    }


def lead_buckets(s: Scored, step_minutes: int, edges_h=(0, 1, 6, 24, 48)) -> dict[str, dict]:
    out = {}
    lead_h = (s.lead_steps + 1) * step_minutes / 60
    for lo, hi in zip(edges_h, edges_h[1:]):
        mask = (lead_h > lo) & (lead_h <= hi)
        if mask.any():
            out[f"{lo}-{hi}h"] = metrics(s, mask)
    return out


def actual_grid(series_name: str, location: str, book: ForecastBook) -> Grid:
    from src.data import series as data

    lo = pd.Timestamp(int(book.issued.min()), tz="UTC") - timedelta(days=9)
    hi = pd.Timestamp(int(book.issued.max()), tz="UTC") + timedelta(days=3)
    raw = data.rt_prices(location, lo, hi) if series_name == "rt_price" else data.system_load(lo, hi)
    return Grid.from_series(raw, book.step_minutes)


def evaluate(cache_dir: Path, model: str, series_name: str, location: str, source: str = "parquet") -> dict:
    book = ForecastBook.load(location, series_name, model=model, source=source, cache_dir=cache_dir)
    if not len(book):
        return {}
    s = score(book, actual_grid(series_name, location, book))
    return {"forecasts": len(book), "all": metrics(s), "by_lead": lead_buckets(s, book.step_minutes)}


def format_row(label: str, m: dict) -> str:
    return (f"| {label} | {m['points']:,} | {m['mae']:.2f} | {m['sn1_mae']:.2f} | {m['sn7_mae']:.2f} | "
            f"{m['medae']:.2f} | {m['pinball']:.2f} | {m['sn1_pinball']:.2f} | {m['sn7_pinball']:.2f} | "
            f"{100 * m['skill_mae_vs_sn1']:+.0f}% | {100 * m['skill_pinball_vs_sn7']:+.0f}% | "
            f"{100 * m['coverage_80']:.0f}% |")


HEADER = ("| run | points | MAE | SN1 MAE | SN7 MAE | MedAE | pinball | SN1 pinball | SN7 pinball | "
          "MAE skill vs SN1 | pinball skill vs SN7 | p10-p90 coverage |\n" + "|---" * 12 + "|")


def main(argv: list[str] | None = None) -> int:
    from src.forecast.settings import load_settings

    settings = load_settings()
    ap = argparse.ArgumentParser(prog="python -m src.forecast.accuracy", description=__doc__.split("\n")[0])
    ap.add_argument("--cache-dir", type=Path, default=settings.cache_dir)
    ap.add_argument("--model", default=settings.model)
    ap.add_argument("--series", nargs="+", default=["rt_price", "demand"])
    ap.add_argument("--locations", nargs="+", default=list(settings.locations))
    ap.add_argument("--source", choices=("db", "parquet"), default=None, help="default: db if configured")
    ap.add_argument("--label", default="")
    ap.add_argument("--by-lead", action="store_true")
    args = ap.parse_args(argv)
    from src.forecast.store import default_source

    source = args.source or default_source()
    print(HEADER)
    for name in args.series:
        locations = args.locations if name == "rt_price" else [settings.demand_location]
        for location in locations:
            r = evaluate(args.cache_dir, args.model, name, location, source)
            if not r:
                continue
            tag = f"{args.label} {name} {location}".strip()
            print(format_row(f"{tag} ({r['forecasts']} fc)", r["all"]))
            if args.by_lead:
                for bucket, m in r["by_lead"].items():
                    print(format_row(f"{tag} lead {bucket}", m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
