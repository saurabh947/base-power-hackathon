"""M0 walking skeleton: one battery, one real day, an in-process tick loop.

Run:  python -m src.sim.skeleton [--strategy bands|peak] [--lag N] [--soc-init X]

Each 5-minute tick of local day 2025-05-16 (America/Chicago) at LZ_HOUSTON runs
    Strategy.target_kw -> allocate -> BatteryAdapter.apply -> read -> Ledger.record
Prices are real ERCOT RT settlement point prices (src/sim/m0_prices.py): native
15-min, forward-filled to 5-min ticks. The strategy sees only the last
*published* price (config sim.decision_lag_ticks, default 3). The bands
strategy's trailing 7-day window is seeded with the 7 real days before.
The ledger prices energy at the RT price of the tick it moves in.

ACCEPT (spec M0): summary()["total_usd"] is nonzero. Otherwise exit code 1.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from src.config import Config, load_config
from src.core.ids import make_command_id
from src.core.models import BatteryState, Command
from src.feeds.memory import InMemoryPriceFeed
from src.ledger.backtest import BacktestLedger
from src.sim import m0_prices
from src.sim.adapters import InMemoryBatteryAdapter
from src.sim.decision import decision_tick
from src.sim.allocation import allocate, fleet_headroom_kw
from src.sim.battery import fleet_soc, grid_kwh_from_soc_change, headroom_kw
from src.sim.fleet import generate_batteries
from src.strategy import STRATEGY_NAMES, make_strategy

CHICAGO = ZoneInfo("America/Chicago")


@dataclass(frozen=True)
class TickRow:
    ts: datetime  # delivery tick, UTC
    price_usd_mwh: float  # RT price the ledger uses for this tick
    seen_price_usd_mwh: float | None  # the lagged price the strategy saw (None: nothing published yet)
    target_kw: float  # strategy output, before clipping
    delivered_kwh: float  # grid-side, + sold / - bought
    soc: float  # after the tick


@dataclass(frozen=True)
class SkeletonRun:
    summary: dict
    start: BatteryState
    final: BatteryState
    strategy: str
    lag_ticks: int
    rows: list[TickRow]


def run(
    cfg: Config | None = None,
    *,
    strategy_name: str | None = None,
    lag: int | None = None,
    soc_init: float | None = None,
) -> SkeletonRun:
    cfg = cfg or load_config()
    lag = cfg.sim.decision_lag_ticks if lag is None else lag
    if lag < 0:
        raise ValueError("lag must be >= 0")
    dt_h = cfg.sim.dt_hours
    soc_min, soc_max = cfg.battery.soc_min, cfg.battery.soc_max
    window_ticks = cfg.strategy.bands.window_days * cfg.sim.ticks_per_day

    feed = InMemoryPriceFeed.from_intervals(m0_prices.interval_rows())
    ticks = list(feed.ticks(m0_prices.WARMUP_START_UTC, m0_prices.DAY_END_UTC, m0_prices.LOCATION))
    prices = [t.price_usd_mwh for t in ticks]
    first = next(i for i, t in enumerate(ticks) if t.ts == m0_prices.DAY_START_UTC)
    if len(ticks) - first != cfg.sim.ticks_per_day or first != len(m0_prices.WARMUP_2025_05_09_TO_15) * 3:
        raise RuntimeError("the M0 prices must be 7 gap-free warm-up days plus one full day")

    (start,) = generate_batteries(1, cfg.battery, cfg.seed, soc_init=soc_init)
    ids = [start.battery_id]  # the one battery sits at LZ_HOUSTON, where the prices are from
    adapter = InMemoryBatteryAdapter([start], tick_seconds=cfg.sim.tick_seconds, soc_min=soc_min, soc_max=soc_max)
    strategy = make_strategy(cfg.strategy, cfg.sim.tick_seconds, strategy_name)
    ledger = BacktestLedger({m0_prices.LOCATION: len(ids)})
    run_id = uuid.uuid4()

    rows = []
    for i in range(first, len(ticks)):
        tick = ticks[i]
        before = [adapter.read(bid) for bid in ids]
        soc = np.array([s.soc for s in before])
        capacity = np.array([s.capacity_kwh for s in before])
        power = np.array([s.power_kw for s in before])
        efficiency = np.array([s.efficiency for s in before])
        available = np.array([s.available for s in before])

        # 1. strategy: decision-time clock, last published price, only earlier history (no peeking)
        seen = i - lag
        if seen >= 0:
            target = strategy.target_kw(decision_tick(tick, ticks[seen]), fleet_soc(soc, capacity, available),
                                        fleet_headroom_kw(power, available),
                                        prices[max(0, seen - window_ticks):seen])
        else:
            target = 0.0
        # 2. clip to feasible directional headroom, split in proportion to headroom
        discharge, charge = headroom_kw(soc, capacity, power, efficiency, available, dt_h, soc_min, soc_max)
        _, setpoints = allocate(target, discharge, charge)
        # 3. one idempotent command per battery
        for bid, setpoint in zip(ids, setpoints):
            adapter.apply(Command(make_command_id(run_id, bid, tick.ts), bid, float(setpoint), tick.ts))
        # 4. meter grid-side energy through read(), then price it at this tick's RT price
        after = [adapter.read(bid) for bid in ids]
        delivered = float(grid_kwh_from_soc_change(soc, [s.soc for s in after], capacity, efficiency).sum())
        ledger.record(tick, delivered)
        rows.append(TickRow(tick.ts, tick.price_usd_mwh, ticks[seen].price_usd_mwh if seen >= 0 else None,
                            target, delivered, after[0].soc))

    return SkeletonRun(ledger.summary(), start, adapter.read(ids[0]), repr(strategy), lag, rows)


def format_report(result: SkeletonRun) -> str:
    b, s = result.start, result.summary
    out = [
        "M0 walking skeleton: 1 battery, real ERCOT RT prices, LZ_HOUSTON, local day 2025-05-16 (America/Chicago)",
        f"battery  {b.battery_id}: {b.power_kw:.2f} kW / {b.capacity_kwh:.2f} kWh, round-trip eta {b.efficiency:.2f},"
        f" SoC {b.soc:.2f} -> {result.final.soc:.2f}",
        f"strategy {result.strategy}, decision lag {result.lag_ticks} ticks",
        "",
        "local hour  avg $/MWh   net kWh (+sold/-bought)       USD   SoC at end",
    ]
    hours: dict[int, list[TickRow]] = {}
    for row in result.rows:
        hours.setdefault(row.ts.astimezone(CHICAGO).hour, []).append(row)
    for hour, rows in hours.items():
        price = sum(r.price_usd_mwh for r in rows) / len(rows)
        kwh = sum(r.delivered_kwh for r in rows)
        usd = sum(r.delivered_kwh * r.price_usd_mwh / 1000.0 for r in rows)
        out.append(f"   {hour:02d}:00  {price:10.2f}  {kwh:+24.3f}  {usd:+8.3f}   {rows[-1].soc:10.3f}")
    out += ["", "Ledger.summary() =", json.dumps(_rounded(s), indent=2), ""]
    verdict = "PASS" if s["total_usd"] != 0 else "FAIL"
    out.append(f"ACCEPT (M0): total_usd = ${s['total_usd']:.4f}, nonzero -> {verdict}")
    return "\n".join(out)


def _rounded(value, digits: int = 6):
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {k: _rounded(v, digits) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v, digits) for v in value]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.sim.skeleton", description=__doc__.split("\n")[0])
    parser.add_argument("--strategy", choices=STRATEGY_NAMES, help="override config strategy.name")
    parser.add_argument("--lag", type=int, help="decision lag in ticks (default: config; 0 = spec-literal)")
    parser.add_argument("--soc-init", type=float, help="initial SoC (default: config battery.soc_init)")
    parser.add_argument("--config", help="path to a config.yaml (default: repo root)")
    args = parser.parse_args(argv)
    result = run(load_config(args.config), strategy_name=args.strategy, lag=args.lag, soc_init=args.soc_init)
    print(format_report(result))
    return 0 if result.summary["total_usd"] != 0 else 1


if __name__ == "__main__":
    sys.exit(main())
