"""Numbers for one replay (one strategy, one decision lag), from the Ledger and the engine.

Definitions (the Results screen and backtest_runs use exactly these):
- fleet total USD = Ledger.summary()["total_usd"]: grid-side kWh x that tick's RT price.
- per battery per year = fleet total / fleet size, over the replayed calendar year.
  `coverage` says how much of the year had prices; nothing is annualised silently.
- $/kW-year = fleet total / sum of power_kw; $/kWh-year = fleet total / sum of capacity_kwh
  (nameplate).
- cycles per year = energy discharged out of storage / usable energy, where usable =
  capacity x (soc_max - soc_min): one cycle empties the battery from 100% to the 20% reserve.
  cycles_per_year_nameplate divides by the full capacity instead.
- top days: fleet USD per America/Chicago operating day (the Ledger's top_spike_days).
"""
from __future__ import annotations

from datetime import date
from typing import Mapping
from zoneinfo import ZoneInfo

import numpy as np

from src.backtest.engine import OPERATING_TZ, Fleet, LocationRun, TickSeries, local_midnight_utc


def fleet_totals(fleets: Mapping[str, Fleet], soc_min: float, soc_max: float) -> dict:
    power = sum(float(f.power_kw.sum()) for f in fleets.values())
    capacity = sum(float(f.capacity_kwh.sum()) for f in fleets.values())
    return {
        "size": sum(f.size for f in fleets.values()),
        "by_location": {loc: f.size for loc, f in fleets.items()},
        "power_kw": power,
        "capacity_kwh": capacity,
        "usable_kwh": capacity * (soc_max - soc_min),
    }


def replay_metrics(
    summary: dict,
    runs: Mapping[str, LocationRun],
    fleets: Mapping[str, Fleet],
    series: Mapping[str, TickSeries],
    *,
    soc_min: float,
    soc_max: float,
    expected_ticks: int,
    tz: str = OPERATING_TZ,
) -> dict:
    """Everything the record stores for one replay. `summary` is Ledger.summary()."""
    totals = fleet_totals(fleets, soc_min, soc_max)
    n = totals["size"]
    total = float(summary["total_usd"])
    zone = ZoneInfo(tz)

    per_location = {}
    storage_out = 0.0
    revenue_all, sell_usd, buy_usd = [], 0.0, 0.0
    monthly: dict[str, float] = {}
    balance_residual = 0.0
    replayed = {}
    for loc, run in runs.items():
        fleet = fleets[loc]
        ticks = series[loc].replayed
        replayed[loc] = len(ticks)
        prices = np.fromiter((t.price_usd_mwh for t in ticks), dtype=np.float64, count=len(ticks))
        root_eta = np.sqrt(fleet.efficiency)
        out = float((run.sold_kwh / root_eta).sum())
        storage_out += out
        usd_tick = run.delivered_kwh * prices / 1000.0
        sell_usd += float(usd_tick[run.delivered_kwh > 0].sum())
        buy_usd += float(-usd_tick[run.delivered_kwh < 0].sum())
        for month, usd in _monthly(ticks, usd_tick, zone).items():
            monthly[month] = monthly.get(month, 0.0) + usd
        # storage balance: stored change = bought * sqrt(eta) - sold / sqrt(eta), per battery
        stored = (run.soc_final - fleet.soc_init) * fleet.capacity_kwh
        balance_residual = max(balance_residual, float(np.abs(
            stored - (run.bought_kwh * root_eta - run.sold_kwh / root_eta)).max(initial=0.0)))
        revenue_all.append(run.revenue_usd)
        usable = float(fleet.capacity_kwh.sum()) * (soc_max - soc_min)
        loc_usd = float(summary["per_location_usd"].get(loc, 0.0))
        per_location[loc] = {
            "batteries": fleet.size,
            "usd": loc_usd,
            "per_battery_usd": loc_usd / fleet.size if fleet.size else 0.0,
            "sold_kwh": float(run.sold_kwh.sum()),
            "bought_kwh": float(run.bought_kwh.sum()),
            "cycles_per_year": out / usable if usable else 0.0,
            "active_ticks": run.active_ticks,
            "replay_seconds": round(run.seconds, 3),
        }
    revenue = np.concatenate(revenue_all) if revenue_all else np.zeros(0)
    sold, bought = float(summary["sold_kwh"]), float(summary["bought_kwh"])
    ticks_replayed = max(replayed.values(), default=0)
    return {
        "per_battery_usd_year": total / n if n else 0.0,
        "fleet_usd_year": total,
        "usd_per_kw_year": total / totals["power_kw"] if totals["power_kw"] else 0.0,
        "usd_per_kwh_year": total / totals["capacity_kwh"] if totals["capacity_kwh"] else 0.0,
        "cycles_per_year": storage_out / totals["usable_kwh"] if totals["usable_kwh"] else 0.0,
        "cycles_per_year_nameplate": storage_out / totals["capacity_kwh"] if totals["capacity_kwh"] else 0.0,
        "sold_kwh": sold,
        "bought_kwh": bought,
        "avg_sell_usd_mwh": sell_usd * 1000.0 / sold if sold else None,
        "avg_buy_usd_mwh": buy_usd * 1000.0 / bought if bought else None,
        "per_location": per_location,
        "per_battery_usd_percentiles": _percentiles(revenue),
        "monthly_usd": dict(sorted(monthly.items())),
        "top_days": top_days(summary),
        "coverage": {
            "ticks_replayed": replayed,
            "ticks_expected": expected_ticks,
            "fraction": min(replayed.values(), default=0) / expected_ticks if expected_ticks else 0.0,
        },
        "checks": {"storage_balance_max_abs_kwh": balance_residual},
        "ticks_replayed": ticks_replayed,
    }


def top_days(summary: dict) -> list[dict]:
    return [{"date_local": d["date"], "usd": float(d["usd"])} for d in summary["top_spike_days"]]


def compact(metrics: dict) -> dict:
    """The headline numbers of a replay, for storing a variant (lag 0) next to the headline."""
    keys = ("per_battery_usd_year", "fleet_usd_year", "usd_per_kw_year", "usd_per_kwh_year", "cycles_per_year",
            "sold_kwh", "bought_kwh", "avg_sell_usd_mwh", "avg_buy_usd_mwh", "top_days")
    out = {k: metrics[k] for k in keys}
    out["per_location_usd"] = {loc: v["usd"] for loc, v in metrics["per_location"].items()}
    return out


def _monthly(ticks, usd_tick: np.ndarray, zone: ZoneInfo) -> dict[str, float]:
    """USD per local calendar month ("YYYY-MM")."""
    if not ticks:
        return {}
    first, last = ticks[0].ts.astimezone(zone), ticks[-1].ts.astimezone(zone)
    months = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    starts = np.array([local_midnight_utc(date(y, m, 1), zone.key).timestamp() for y, m in months])
    epoch = np.fromiter((t.ts.timestamp() for t in ticks), dtype=np.float64, count=len(ticks))
    idx = np.searchsorted(starts, epoch, side="right") - 1
    sums = np.bincount(idx, weights=usd_tick, minlength=len(months))
    return {f"{y:04d}-{m:02d}": float(s) for (y, m), s in zip(months, sums)}


def _percentiles(values: np.ndarray) -> dict:
    if len(values) == 0:
        return {}
    qs = (0, 10, 50, 90, 100)
    return {f"p{q}": float(v) for q, v in zip(qs, np.percentile(values, qs))}
