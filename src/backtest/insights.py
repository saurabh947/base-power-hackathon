"""What the AI forecast adds, measured on a full year: money, trade quality, grid support, surplus power.

Replays each strategy with the backtest engine (the same code as the pitch run) and
reads the per-tick fleet energy against real ERCOT prices, ERCOT load and the fuel mix:

- trade quality: average price sold at, average price bought at, net $ per MWh sold
- scarcity:      energy delivered while prices were >= $1,000/MWh (the grid's tightest
                 minutes), and how charged the fleet was when each such event began
- peak support:  average MW delivered during ERCOT's top 1% load hours of the year
- surplus:       MWh bought at prices <= $0, and the wind+solar share of charging energy

Run:  python -m src.backtest.insights --year 2025 [--strategies bands,forecast,peak] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

from src.backtest.runner import (
    StrategyUnavailable,
    backtest_settings,
    build_context,
    forecast_coverage,
    make_named_strategy,
    replay,
)
from src.data import series as data

SCARCITY_USD_MWH = 1000.0
PEAK_LOAD_SHARE = 0.01


def _tick_index(ticks) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([t.ts for t in ticks]).tz_convert("UTC")


def measure(name: str, ctx) -> dict:
    cfg = ctx.cfg
    strategies = {loc: make_named_strategy(name, cfg, loc) for loc in ctx.fleets}
    need = float(ctx.settings.get("forecast_min_coverage", 0.99))
    for loc, strategy in strategies.items():
        cov = forecast_coverage(strategy, loc, ctx.series[loc].replayed)
        if cov is not None and cov < need:
            raise StrategyUnavailable(f"forecasts cover {cov:.1%} at {loc}")
    run = replay(name, cfg, ctx.series, ctx.fleets, lag=cfg.sim.decision_lag_ticks, fast=True,
                 expected_ticks=ctx.expected_ticks, window_cache=ctx.window_cache, strategies=strategies)
    dt_h = cfg.sim.dt_hours
    n_batteries = sum(f.size for f in ctx.fleets.values())
    fleet_power_mw = sum(float(f.power_kw[f.available].sum()) for f in ctx.fleets.values()) / 1000.0

    # ERCOT-wide context per tick: hourly load and the 15-min wind+solar share
    idx = _tick_index(next(iter(ctx.series.values())).replayed)
    load = data.system_load(idx[0] - pd.Timedelta(hours=1), idx[-1] + pd.Timedelta(hours=1))
    load_at = load.reindex(idx.floor("h")).to_numpy()
    mix = data.fuel_mix(idx[0] - pd.Timedelta(minutes=15), idx[-1] + pd.Timedelta(minutes=15))
    renew = (mix.get("wind", 0) + mix.get("solar", 0))
    renew_at = renew.reindex(idx.floor("15min")).to_numpy()
    peak_cut = np.nanquantile(load.to_numpy(), 1 - PEAK_LOAD_SHARE)
    peak_mask = load_at >= peak_cut

    tot = dict(sold_mwh=0.0, bought_mwh=0.0, sell_usd=0.0, buy_usd=0.0, scarcity_mwh=0.0,
               scarcity_ticks=0, surplus_mwh=0.0, charge_renew_mwh=0.0, charge_known_mwh=0.0)
    fleet_mw = np.zeros(len(idx))
    readiness = []
    for loc, lr in run.runs.items():
        ticks = ctx.series[loc].replayed
        price = np.fromiter((t.price_usd_mwh for t in ticks), float, len(ticks))
        d = np.asarray(lr.delivered_kwh, float)  # + sold, - bought, fleet kWh per tick
        assert len(d) == len(price) == len(idx), (loc, len(d), len(price), len(idx))
        sold, bought = np.clip(d, 0, None), np.clip(-d, 0, None)
        tot["sold_mwh"] += sold.sum() / 1000
        tot["bought_mwh"] += bought.sum() / 1000
        tot["sell_usd"] += float((sold * price).sum() / 1000)
        tot["buy_usd"] += float((bought * price).sum() / 1000)
        scarce = price >= SCARCITY_USD_MWH
        tot["scarcity_mwh"] += sold[scarce].sum() / 1000
        tot["scarcity_ticks"] += int(scarce.sum())
        # charge level of this city's fleet just before each scarcity event begins
        starts = np.flatnonzero(scarce & ~np.r_[False, scarce[:-1]])
        soc = np.asarray(lr.fleet_soc, float)
        readiness += [float(soc[i - 1]) for i in starts if i > 0]
        tot["surplus_mwh"] += bought[price <= 0].sum() / 1000
        known = ~np.isnan(renew_at)
        tot["charge_renew_mwh"] += float((bought[known] * renew_at[known]).sum() / 1000)
        tot["charge_known_mwh"] += float(bought[known].sum() / 1000)
        fleet_mw += d / dt_h / 1000.0

    net = tot["sell_usd"] - tot["buy_usd"]
    return {
        "strategy": name,
        "batteries": n_batteries,
        "fleet_power_mw": round(fleet_power_mw, 1),
        "revenue_usd": round(net, 2),
        "per_battery_usd": round(net / n_batteries, 2),
        "sold_mwh": round(tot["sold_mwh"], 1),
        "bought_mwh": round(tot["bought_mwh"], 1),
        "avg_sell_usd_mwh": round(tot["sell_usd"] / max(tot["sold_mwh"], 1e-9), 2),
        "avg_buy_usd_mwh": round(tot["buy_usd"] / max(tot["bought_mwh"], 1e-9), 2),
        "net_usd_per_mwh_sold": round(net / max(tot["sold_mwh"], 1e-9), 2),
        "scarcity_mwh": round(tot["scarcity_mwh"], 1),
        "scarcity_intervals_5min": tot["scarcity_ticks"],
        "scarcity_readiness_avg_charge": round(float(np.mean(readiness)), 3) if readiness else None,
        "scarcity_events": len(readiness),
        "peak_hours": int(np.unique(idx[peak_mask].floor("h")).size),
        "peak_support_avg_mw": round(float(np.clip(fleet_mw[peak_mask], 0, None).mean()), 1),
        "peak_support_share_of_fleet_power": round(float(np.clip(fleet_mw[peak_mask], 0, None).mean()) / fleet_power_mw, 3),
        "surplus_absorbed_mwh": round(tot["surplus_mwh"], 1),
        "charging_renewable_share": round(tot["charge_renew_mwh"] / max(tot["charge_known_mwh"], 1e-9), 3),
        "grid_renewable_share": round(float(np.nanmean(renew_at)), 3),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--year", type=int, default=2025)
    p.add_argument("--strategies", default="bands,forecast,peak")
    p.add_argument("--json")
    args = p.parse_args(argv)
    ctx = build_context(args.year, settings=backtest_settings())
    out = {"year": args.year, "strategies": {}}
    for name in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        try:
            out["strategies"][name] = measure(name, ctx)
        except StrategyUnavailable as exc:
            out["strategies"][name] = {"strategy": name, "skipped": str(exc)}
        print(json.dumps(out["strategies"][name]), flush=True)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
