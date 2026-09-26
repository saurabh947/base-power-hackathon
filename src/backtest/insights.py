"""What the AI forecast adds, measured on a full year: money, trade quality, spikes, surplus power.

Two layers:

- `from_replay(replay, series)`: the numbers every strategy record stores as `metrics.insights`, served
  by GET /results/ai-gains (docs/API.md StrategyGains). They come from the Replay the runner already has
  (each city's grid-side kWh per 5-minute tick) and the replayed ticks' RT prices, so they cost no
  extra replay:
    sold_mwh, bought_mwh               energy sold to / bought from the grid
    avg_sell_usd_mwh, avg_buy_usd_mwh  the average price sold at / bought at
    surplus_mwh                        energy bought at prices <= $0/MWh (surplus power soaked up)
    spike_mwh                          energy sold while the 5-minute price was >= $1,000/MWh
    spike_events                       spikes: a city's price crossing $1,000/MWh from below. Counted per
                                       city, so a spike that hits all four cities at once counts 4
    spike_charge_level                 that city's fleet charge level (capacity-weighted SoC) just before
                                       each spike's first 5-minute step, averaged over spike_events
                                       (None when there were none). A spike at the very first replayed
                                       step has no "just before" and is not counted.
    spike_intervals_5min, spike_usd_mwh  city-steps at or above the threshold, and the threshold
    revenue_usd                        sold x price - bought x price: the same money as the Ledger
- `grid_context(...)`: the CLI's extra numbers from ERCOT load and the fuel mix (support during ERCOT's
  top 1% load hours; the wind+solar share of charging energy). Not stored and not served: the human
  dropped the peak-load angle. They need load and fuel-mix data for the year (Postgres, or the 2025
  snapshot); without it they are None.

Run:
  python -m src.backtest.insights --year 2025 [--strategies bands,forecast,peak] [--json out.json]
  python -m src.backtest.insights --year 2025 --daily [--pair bands,forecast] [--top 10] [--json out.json]

--daily prints, per local (Central time) day, the fleet's revenue for the pair's two strategies over the
whole day and over the evening (18:00-24:00), and the top days by challenger minus baseline for each.
It also replays each top day as the live race would show it: both strategies from the same start time
(midnight, or 6 pm for the evening) with the fleet at the demo's starting charge.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from bisect import bisect_left
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from src.backtest.engine import OPERATING_TZ, TickSeries, build_fleets, local_midnight_utc

SPIKE_USD_MWH = 1000.0  # docs/API.md AiGains: "Spikes are 5-minute prices >= $1,000/MWh"
SURPLUS_USD_MWH = 0.0  # surplus power: bought at prices <= $0/MWh
PEAK_LOAD_SHARE = 0.01  # CLI only: ERCOT's top 1% load hours
EVENING = (dtime(18, 0), dtime(0, 0))  # --daily: 6 pm to midnight, Central time
UTC = timezone.utc


def _prices(ticks) -> np.ndarray:
    return np.fromiter((t.price_usd_mwh for t in ticks), dtype=np.float64, count=len(ticks))


# --- stored with every record: metrics.insights ----------------------------------------------------


def from_replay(replay, series: Mapping[str, TickSeries], *, spike_usd_mwh: float = SPIKE_USD_MWH) -> dict:
    """metrics.insights for one Replay (src.backtest.runner.Replay, or anything with `.runs`).

    Reads each city's per-tick delivered kWh (+ sold, - bought) and fleet charge level from the replay
    and the replayed ticks' prices from `series`. Plain floats, unrounded (the API rounds).
    """
    sold_mwh = bought_mwh = sell_usd = buy_usd = surplus_mwh = spike_mwh = 0.0
    spike_steps = 0
    readiness: list[float] = []
    for loc, run in replay.runs.items():
        price = _prices(series[loc].replayed)
        d = np.asarray(run.delivered_kwh, dtype=np.float64)
        if len(d) != len(price):
            raise ValueError(f"{loc}: {len(d)} replayed ticks but {len(price)} prices")
        sold, bought = np.clip(d, 0.0, None), np.clip(-d, 0.0, None)
        sold_mwh += float(sold.sum()) / 1000.0
        bought_mwh += float(bought.sum()) / 1000.0
        sell_usd += float((sold * price).sum()) / 1000.0
        buy_usd += float((bought * price).sum()) / 1000.0
        surplus_mwh += float(bought[price <= SURPLUS_USD_MWH].sum()) / 1000.0
        spike = price >= spike_usd_mwh
        spike_mwh += float(sold[spike].sum()) / 1000.0
        spike_steps += int(spike.sum())
        starts = np.flatnonzero(spike & ~np.r_[False, spike[:-1]])
        level = np.asarray(run.fleet_soc, dtype=np.float64)  # after each tick: [i - 1] is just before tick i
        readiness += [float(level[i - 1]) for i in starts if i > 0]
    return {
        "revenue_usd": sell_usd - buy_usd,
        "sold_mwh": sold_mwh,
        "bought_mwh": bought_mwh,
        "avg_sell_usd_mwh": sell_usd / sold_mwh if sold_mwh > 0 else None,
        "avg_buy_usd_mwh": buy_usd / bought_mwh if bought_mwh > 0 else None,
        "surplus_mwh": surplus_mwh,
        "spike_mwh": spike_mwh,
        "spike_events": len(readiness),
        "spike_charge_level": float(np.mean(readiness)) if readiness else None,
        "spike_intervals_5min": spike_steps,
        "spike_usd_mwh": float(spike_usd_mwh),
    }


# --- CLI only: grid context -------------------------------------------------------------------------


def grid_context(replay, series: Mapping[str, TickSeries], fleet_power_mw: float, dt_h: float) -> dict:
    """Peak-load support and the renewable share of charging (ERCOT load and fuel mix). None without data."""
    import pandas as pd

    from src.data import series as data

    first = next(iter(series.values())).replayed
    idx = pd.DatetimeIndex([t.ts for t in first]).tz_convert("UTC")
    out = {"peak_hours": None, "peak_support_avg_mw": None, "peak_support_share_of_fleet_power": None,
           "charging_renewable_share": None, "grid_renewable_share": None}
    fleet_mw = np.zeros(len(idx))
    bought = np.zeros(len(idx))
    for loc, run in replay.runs.items():
        d = np.asarray(run.delivered_kwh, dtype=np.float64)
        if not len(d) == len(series[loc].replayed) == len(idx):
            raise ValueError(f"grid context needs every city on the same ticks ({loc}: {len(d)} vs {len(idx)})")
        fleet_mw += d / dt_h / 1000.0
        bought += np.clip(-d, 0.0, None)
    try:
        load = data.system_load(idx[0] - pd.Timedelta(hours=1), idx[-1] + pd.Timedelta(hours=1))
    except Exception:
        load = None
    if load is not None and len(load.dropna()):
        load_at = load.reindex(idx.floor("h")).to_numpy()
        cut = np.nanquantile(load.to_numpy(), 1 - PEAK_LOAD_SHARE)
        peak = load_at >= cut
        if peak.any():
            support = float(np.clip(fleet_mw[peak], 0, None).mean())
            out.update(peak_hours=int(np.unique(idx[peak].floor("h")).size), peak_support_avg_mw=support,
                       peak_support_share_of_fleet_power=support / fleet_power_mw if fleet_power_mw else None)
    try:
        mix = data.fuel_mix(idx[0] - pd.Timedelta(minutes=15), idx[-1] + pd.Timedelta(minutes=15))
    except Exception:
        mix = None
    if mix is not None and len(mix):
        renew = (mix.get("wind", 0) + mix.get("solar", 0)).reindex(idx.floor("15min")).to_numpy(dtype=np.float64)
        known = ~np.isnan(renew)
        if known.any() and bought[known].sum() > 0:
            out.update(charging_renewable_share=float((bought[known] * renew[known]).sum() / bought[known].sum()),
                       grid_renewable_share=float(np.nanmean(renew)))
    return out


def _strategies(name: str, ctx) -> dict:
    """One instance per city, refused (StrategyUnavailable) when its forecasts don't cover the year."""
    from src.backtest.runner import ForecastsMissing, forecast_coverage, make_named_strategy

    strategies = {loc: make_named_strategy(name, ctx.cfg, loc) for loc in ctx.fleets}
    need = float(ctx.settings.get("forecast_min_coverage", 0.99))
    short = {}
    for loc, strategy in strategies.items():
        cov = forecast_coverage(strategy, loc, ctx.series[loc].replayed)
        if cov is not None and cov < need:
            short[loc] = cov
    if short:
        where = ", ".join(f"{cov:.1%} at {loc}" for loc, cov in short.items())
        raise ForecastsMissing(f"forecasts cover {where} of {ctx.year} (need {need:.0%} in every city)", list(short),
                               ctx.year)
    return strategies


def _replay(name: str, ctx, strategies=None):
    from src.backtest.runner import replay

    return replay(name, ctx.cfg, ctx.series, ctx.fleets, lag=ctx.cfg.sim.decision_lag_ticks, fast=True,
                  expected_ticks=ctx.expected_ticks, window_cache=ctx.window_cache,
                  strategies=strategies if strategies is not None else _strategies(name, ctx))


def measure(name: str, ctx, *, strategies=None) -> dict:
    """The CLI's line for one strategy: the stored insights, the ledger's money, and the grid context."""
    run = _replay(name, ctx, strategies)
    n = sum(f.size for f in ctx.fleets.values())
    power_mw = sum(float(f.power_kw[f.available].sum()) for f in ctx.fleets.values()) / 1000.0
    ins = from_replay(run, ctx.series)
    total = float(run.summary["total_usd"])

    def r(x, digits):
        return None if x is None else round(float(x), digits)

    out = {
        "strategy": name,
        "batteries": n,
        "fleet_power_mw": round(power_mw, 1),
        "revenue_usd": round(total, 2),  # the Ledger's, as the pitch backtest books it
        "per_battery_usd": round(total / n, 2) if n else None,
        "insights_revenue_usd": round(ins["revenue_usd"], 2),  # from_replay's own sum: the same money
        "sold_mwh": r(ins["sold_mwh"], 1),
        "bought_mwh": r(ins["bought_mwh"], 1),
        "avg_sell_usd_mwh": r(ins["avg_sell_usd_mwh"], 2),
        "avg_buy_usd_mwh": r(ins["avg_buy_usd_mwh"], 2),
        "net_usd_per_mwh_sold": r(total / ins["sold_mwh"], 2) if ins["sold_mwh"] else None,
        "surplus_mwh": r(ins["surplus_mwh"], 1),
        "spike_mwh": r(ins["spike_mwh"], 1),
        "spike_events": ins["spike_events"],
        "spike_charge_level": r(ins["spike_charge_level"], 3),
        "spike_intervals_5min": ins["spike_intervals_5min"],
    }
    ctx_numbers = grid_context(run, ctx.series, power_mw, ctx.cfg.sim.dt_hours)
    out.update({k: (r(v, 3) if isinstance(v, float) else v) for k, v in ctx_numbers.items()})
    return out


# --- --daily: pick the demo day for the live race ------------------------------------------------


def _local_times(ticks, zone: ZoneInfo) -> tuple[list[str], np.ndarray]:
    """(local date 'YYYY-MM-DD', minutes after local midnight) per tick."""
    dates, minutes = [], np.empty(len(ticks), dtype=np.int32)
    for j, t in enumerate(ticks):
        local = t.ts.astimezone(zone)
        dates.append(local.date().isoformat())
        minutes[j] = local.hour * 60 + local.minute
    return dates, minutes


def daily_revenue(replays: Mapping[str, object], series: Mapping[str, TickSeries], *,
                  tz: str = OPERATING_TZ, evening: tuple[dtime, dtime] = EVENING) -> dict[str, dict]:
    """{date: {"<name>": fleet USD, "<name>_evening": fleet USD, "top_price": {...}, "top_price_evening": {...}}}
    per local day. top_price: the highest 5-minute price in any city, with the city and the local time.

    Each (tick, city) is booked as the Ledger books it (kWh x that tick's price / 1000) on its own local
    date; the evening is [18:00, 24:00) local time.
    """
    zone = ZoneInfo(tz)
    lo = evening[0].hour * 60 + evening[0].minute
    hi = (evening[1].hour * 60 + evening[1].minute) or 24 * 60
    days: dict[str, dict] = {}
    for loc, ts in series.items():
        ticks = ts.replayed
        price = _prices(ticks)
        dates, minutes = _local_times(ticks, zone)
        in_evening = (minutes >= lo) & (minutes < hi)
        keys = np.array(dates)
        uniq, inv = np.unique(keys, return_inverse=True)
        for name, rep in replays.items():
            usd = np.asarray(rep.runs[loc].delivered_kwh, dtype=np.float64) * price / 1000.0
            whole = np.bincount(inv, weights=usd, minlength=len(uniq))
            eve = np.bincount(inv, weights=np.where(in_evening, usd, 0.0), minlength=len(uniq))
            for j, day in enumerate(uniq.tolist()):
                row = days.setdefault(day, {})
                row[name] = row.get(name, 0.0) + float(whole[j])
                row[f"{name}_evening"] = row.get(f"{name}_evening", 0.0) + float(eve[j])
        for j, day in enumerate(uniq.tolist()):  # the highest price of the day and of its evening: where and when
            for key, sel in (("top_price", inv == j), ("top_price_evening", (inv == j) & in_evening)):
                if not sel.any():
                    continue
                k = int(np.flatnonzero(sel)[np.argmax(price[sel])])
                best = days[day].get(key)
                if best is None or price[k] > best["usd_mwh"]:
                    local = ticks[k].ts.astimezone(zone)
                    days[day][key] = {"usd_mwh": float(price[k]), "location": loc, "local_time": local.strftime("%H:%M")}
    return dict(sorted(days.items()))


def race_window(ctx, strategies: Mapping[str, Mapping], start: datetime, end: datetime, soc0: float,
                fleets: Mapping | None = None) -> dict:
    """{name: fleet USD} for each strategy replayed over [start, end) from the same start state, as the live
    race runs: the fleet at `soc0`, the strategy at the live decision lag, and a warm-up of exactly
    strategy.bands.window_days before `start` (the coordinator's PriceWindow)."""
    from src.backtest.runner import replay

    cfg = ctx.cfg
    warmup = timedelta(days=cfg.strategy.bands.window_days)
    window = {}
    for loc, ts in ctx.series.items():
        stamps = [t.ts for t in ts.ticks]
        a, s, e = bisect_left(stamps, start - warmup), bisect_left(stamps, start), bisect_left(stamps, end)
        window[loc] = TickSeries(loc, ts.ticks[a:e], s - a)
    if fleets is None:
        fleets = build_fleets(sum(f.size for f in ctx.fleets.values()), cfg.fleet.locations, cfg.battery, cfg.seed,
                              soc_init=soc0)
    return {name: float(replay(name, cfg, window, fleets, lag=cfg.sim.decision_lag_ticks, fast=True,
                               strategies=strategies[name]).summary["total_usd"])
            for name in strategies}


def daily(ctx, pair: Sequence[str] = ("bands", "forecast"), *, top: int = 10, soc0: float | None = None,
          strategies: Mapping[str, Mapping] | None = None) -> dict:
    """Per local day revenue for the pair (baseline, challenger), and the top days by challenger - baseline."""
    baseline, challenger = pair
    strategies = dict(strategies or {})
    for name in pair:
        if name not in strategies:
            strategies[name] = _strategies(name, ctx)
    replays = {name: _replay(name, ctx, strategies[name]) for name in pair}
    days = daily_revenue(replays, ctx.series)
    soc0 = float(ctx.cfg.battery.demo_soc_init if soc0 is None else soc0)
    cfg = ctx.cfg
    race_fleets = build_fleets(sum(f.size for f in ctx.fleets.values()), cfg.fleet.locations, cfg.battery, cfg.seed,
                               soc_init=soc0)
    zone = ZoneInfo(OPERATING_TZ)
    out = {"year": ctx.year, "baseline": baseline, "challenger": challenger,
           "batteries": sum(f.size for f in ctx.fleets.values()), "race_start_charge_level": soc0,
           "year_total_usd": {name: float(rep.summary["total_usd"]) for name, rep in replays.items()},
           "days": days, "top": {}}
    for window, suffix in (("day", ""), ("evening", "_evening")):
        ranked = sorted(days, key=lambda d: days[d][challenger + suffix] - days[d][baseline + suffix], reverse=True)
        rows = []
        for day in ranked[:top]:
            d = date.fromisoformat(day)
            if window == "day":
                start, end = local_midnight_utc(d), local_midnight_utc(d + timedelta(days=1))
            else:
                start = datetime.combine(d, EVENING[0], tzinfo=zone).astimezone(UTC)
                end = local_midnight_utc(d + timedelta(days=1))
            race = race_window(ctx, {name: strategies[name] for name in pair}, start, end, soc0, race_fleets)
            row = days[day]
            rows.append({
                "date_local": day, "weekday": d.strftime("%a"),
                baseline: row[baseline + suffix], challenger: row[challenger + suffix],
                "lead_usd": row[challenger + suffix] - row[baseline + suffix],
                "race_start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "race": race, "race_lead_usd": race[challenger] - race[baseline],
                "top_price": row.get("top_price" + suffix) or row["top_price"],
            })
        out["top"][window] = rows
    return out


def format_daily(result: dict, labels: Mapping[str, str] | None = None) -> str:
    from src.sim import geo

    labels = labels or {}
    b, c = result["baseline"], result["challenger"]
    soc0 = result["race_start_charge_level"]
    totals = result.get("year_total_usd") or {}
    lines = [
        f"Daily fleet revenue, {result['year']}: {labels.get(c, c)} ({c}) vs {labels.get(b, b)} ({b}), "
        f"{result['batteries']:,} batteries, gross $ at real-time prices.",
        "  year:  the full-year replay (each day starts where the year left the batteries)"
        + (f"; year totals {b} {_usd(totals[b])}, {c} {_usd(totals[c])}." if b in totals and c in totals else "."),
        f"  race:  both fleets start that day at {soc0:.0%} charge at the window's start, as the live race does.",
    ]
    titles = {"day": "whole day (midnight to midnight, Central time)",
              "evening": "evening (6 pm to midnight, Central time)"}
    for window in ("day", "evening"):
        rows = result["top"].get(window) or []
        lines += ["", f"Top {len(rows)} days by {c} minus {b}, {titles[window]}",
                  f"  {'date':<10} {'day':<3} {'year: ' + b:>14} {'year: ' + c:>14} {'year lead':>11} "
                  f"{'race: ' + b:>14} {'race: ' + c:>14} {'race lead':>11}  top price"]
        for r in rows:
            p = r["top_price"]
            lines.append(
                f"  {r['date_local']:<10} {r['weekday']:<3} {_usd(r[b]):>14} {_usd(r[c]):>14} {_usd(r['lead_usd']):>11} "
                f"{_usd(r['race'][b]):>14} {_usd(r['race'][c]):>14} {_usd(r['race_lead_usd']):>11}  "
                f"${p['usd_mwh']:,.0f}/MWh at {_clock(p['local_time'])} in {geo.city_name(p['location'])}")
    return "\n".join(lines)


def _usd(x: float) -> str:
    v = round(x)
    return f"{'-' if v < 0 else ''}${abs(v):,.0f}"


def _clock(hhmm: str) -> str:
    """'19:45' -> '7:45 pm' (Central time, as the website says it)."""
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}:{m:02d} {'am' if h < 12 else 'pm'}"


# --- CLI ----------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from src.backtest.runner import StrategyUnavailable, backtest_settings, build_context

    p = argparse.ArgumentParser(prog="python -m src.backtest.insights", description=__doc__.splitlines()[0])
    p.add_argument("--year", type=int, default=2025)
    p.add_argument("--strategies", default="bands,forecast,peak", help="without --daily: the strategies to measure")
    p.add_argument("--daily", action="store_true", help="per-day revenue and the top days for the live race")
    p.add_argument("--pair", default="bands,forecast", help="with --daily: baseline,challenger (default bands,forecast)")
    p.add_argument("--top", type=int, default=10, help="with --daily: how many days to list per window")
    p.add_argument("--fleet-size", type=int, help="override fleet.size (e.g. a quick run)")
    p.add_argument("--json", help="also write the full output here")
    args = p.parse_args(argv)
    pair = [s.strip() for s in args.pair.split(",") if s.strip()]
    if args.daily and (len(pair) != 2 or pair[0] == pair[1]):
        p.error("--pair needs two different strategies: baseline,challenger")
    settings = backtest_settings()
    ctx = build_context(args.year, settings=settings, fleet_size=args.fleet_size)
    if args.daily:
        try:
            result = daily(ctx, pair, top=args.top)
        except StrategyUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(format_daily(result, settings.get("labels")), flush=True)
        out = result
    else:
        out = {"year": args.year, "strategies": {}}
        for name in [s.strip() for s in args.strategies.split(",") if s.strip()]:
            try:
                out["strategies"][name] = measure(name, ctx)
            except StrategyUnavailable as exc:
                out["strategies"][name] = {"strategy": name, "skipped": str(exc)}
            print(json.dumps(out["strategies"][name]), flush=True)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1, allow_nan=False, default=_json_default)
    return 0


def _json_default(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"not JSON-serialisable: {type(value).__name__}")


if __name__ == "__main__":
    sys.exit(main())
