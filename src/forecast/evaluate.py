"""Small-fleet evaluation of the forecast strategy against bands and peak, on real 2025 prices.

    python -m src.forecast.evaluate [--cache-dir DIR] [--windows 2025-04-01:61 ...] [--batteries 100]

Not the backtest runner (that is src/ledger + the backtest owner's M4): a
plain in-process loop over real ERCOT RT prices, reusing the project's
physics (src.sim.battery), allocation (src.sim.allocation), decision lag
(src.sim.decision.decision_tick) and ledger (src.ledger.backtest). Batteries
come from src.sim.fleet.generate_batteries: the first half steer
LZ_HOUSTON, the second half LZ_NORTH, one Strategy instance per location.

Strategies compared on identical windows, fleet and start SoC:
  bands, peak          the v1 strategies from config.yaml
  forecast             ForecastStrategy on the cached TimesFM forecasts
  forecast/naive       the same rule fed "same time yesterday" (real past prices)
  forecast/oracle      the same rule fed the actual future prices. It peeks, so
                       it is a reference for the rule, never a result.
"""
from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import Config, load_config
from src.core.models import PriceTick
from src.forecast.inputs import Grid
from src.forecast.provider import ForecastBook
from src.ledger.backtest import BacktestLedger
from src.sim.allocation import allocate, fleet_headroom_kw
from src.sim.battery import fleet_soc, headroom_kw, step
from src.sim.decision import decision_tick
from src.sim.fleet import generate_batteries
from src.strategy import make_strategy
from src.strategy.forecast import ForecastStrategy, fleet_duration_h

LOCATIONS = ("LZ_HOUSTON", "LZ_NORTH")
DEFAULT_WINDOWS = ("2025-01-06:30", "2025-04-01:61", "2025-07-01:31", "2025-10-15:30")
FIVE_NS = 5 * 60 * 10**9


@dataclass(frozen=True)
class Window:
    start: date
    days: int

    @classmethod
    def parse(cls, text: str) -> Window:
        day, days = text.split(":")
        return cls(date.fromisoformat(day), int(days))

    def bounds(self, tz: str = "America/Chicago") -> tuple[pd.Timestamp, pd.Timestamp]:
        from src.forecast.job import local_midnight

        return local_midnight(self.start, tz), local_midnight(self.start + timedelta(days=self.days), tz)


@dataclass(frozen=True)
class Prices:
    """Real RT prices for one location, forward-filled 15 -> 5 min within each interval."""

    location: str
    grid: Grid  # native 15-min
    t0_ns: int  # ns of tick 0
    price: np.ndarray  # 5-min ticks

    @classmethod
    def load(cls, location: str, start: pd.Timestamp, end: pd.Timestamp, warmup_days: int = 8) -> Prices:
        from src.data import series

        raw = series.rt_prices(location, start - timedelta(days=warmup_days), end + timedelta(days=2))
        grid = Grid.from_series(raw, 15)
        return cls(location, grid, grid.t0.as_unit("ns").value, np.repeat(grid.values.astype(np.float64), 3))

    def index(self, ts: pd.Timestamp) -> int:
        return (ts.as_unit("ns").value - self.t0_ns) // FIVE_NS

    def tick(self, i: int) -> PriceTick:
        ts = pd.Timestamp(self.t0_ns + i * FIVE_NS, tz="UTC").to_pydatetime()
        return PriceTick(ts, self.location, float(self.price[i]), None, None)


def reference_book(prices: Prices, start: pd.Timestamp, end: pd.Timestamp, kind: str) -> ForecastBook:
    """Hourly 24-h 'forecasts' built from real prices: 'oracle' (the future) or 'naive' (yesterday)."""
    g = prices.grid
    issues = pd.date_range(start, end, freq="h", inclusive="left")
    first = np.array([g.ended_before(t) for t in issues])  # grid index of each forecast's first point
    offset = 0 if kind == "oracle" else -96  # naive: the same 96 quarter-hours one day earlier
    idx = first[:, None] + np.arange(96)[None, :] + offset
    vals = np.where((idx >= 0) & (idx < len(g.values)), g.values[np.clip(idx, 0, len(g.values) - 1)], np.nan)
    vals = vals.astype(np.float32)
    starts = np.array([g.ts(i).as_unit("ns").value for i in first], dtype=np.int64)
    return ForecastBook(prices.location, "rt_price", f"reference-{kind}", 15,
                        issues.as_unit("ns").asi8.astype(np.int64), starts, vals, vals, vals)


def simulate(strategy, prices: Prices, start: pd.Timestamp, end: pd.Timestamp, batteries, cfg: Config) -> dict:
    """In-process tick loop for one location. Returns ledger totals per battery."""
    lag, dt_h = cfg.sim.decision_lag_ticks, cfg.sim.dt_hours
    window_ticks = cfg.strategy.bands.window_days * cfg.sim.ticks_per_day
    soc_min, soc_max = cfg.battery.soc_min, cfg.battery.soc_max
    cap = np.array([b.capacity_kwh for b in batteries])
    power = np.array([b.power_kw for b in batteries])
    eff = np.array([b.efficiency for b in batteries])
    avail = np.ones(len(batteries), dtype=bool)
    soc = np.full(len(batteries), cfg.battery.soc_init)
    ledger = BacktestLedger({prices.location: len(batteries)})
    H = fleet_headroom_kw(power, avail)
    i0, i1 = prices.index(start), prices.index(end)
    history = prices.price
    actions = {"sell": 0, "buy": 0, "hold": 0}
    for i in range(i0, i1):
        seen = i - lag
        now = prices.tick(i)
        target = strategy.target_kw(decision_tick(now, prices.tick(seen)), fleet_soc(soc, cap, avail), H,
                                    history[max(0, seen - window_ticks):seen].tolist())
        actions["sell" if target > 0 else "buy" if target < 0 else "hold"] += 1
        dis, chg = headroom_kw(soc, cap, power, eff, avail, dt_h, soc_min, soc_max)
        _, setpoints = allocate(target, dis, chg)
        soc, delivered = step(soc, cap, power, eff, avail, setpoints, dt_h, soc_min, soc_max)
        if math.isfinite(now.price_usd_mwh):
            ledger.record(now, float(delivered.sum()) * dt_h)
    s = ledger.summary()
    usable = float((cap * (soc_max - soc_min)).sum())
    return {
        "usd_per_battery": s["per_battery_avg_usd"],
        "sold_kwh": s["sold_kwh"] / len(batteries),
        "bought_kwh": s["bought_kwh"] / len(batteries),
        "cycles": s["sold_kwh"] / usable,
        "top_days": s["top_spike_days"][:3],
        "actions": actions,
    }


def evaluate(windows: list[Window], cache_dir: Path | None, n_batteries: int, names: list[str],
             model: str | None = None, cfg: Config | None = None) -> dict:
    cfg = cfg or load_config()
    fleet = generate_batteries(n_batteries, cfg.battery, cfg.seed)
    half = n_batteries // 2
    cohorts = {"LZ_HOUSTON": fleet[:half], "LZ_NORTH": fleet[half:2 * half]}
    results: dict[str, dict] = {}
    for w in windows:
        start, end = w.bounds()
        for location, batteries in cohorts.items():
            prices = Prices.load(location, start, end)
            books = {
                "forecast": lambda: ForecastBook.load(location, model=model, source="parquet", cache_dir=cache_dir,
                                                      start=start - timedelta(hours=7), end=end),
                "forecast/naive": lambda: reference_book(prices, start - timedelta(hours=1), end, "naive"),
                "forecast/oracle": lambda: reference_book(prices, start - timedelta(hours=1), end, "oracle"),
            }
            for name in names:
                if name in books:
                    book = books[name]()
                    if not len(book):
                        raise SystemExit(f"no cached forecasts for {location} in {w}; run src.forecast.job first")
                    strategy = _forecast_strategy({location: book}, batteries, cfg)
                else:
                    strategy = make_strategy(cfg.strategy, cfg.sim.tick_seconds, name)
                r = simulate(strategy, prices, start, end, batteries, cfg)
                results.setdefault(name, {}).setdefault(str(w.start), {})[location] = r
    return results


def _forecast_strategy(books, batteries, cfg: Config) -> ForecastStrategy:
    from src.forecast.settings import load_settings

    st = load_settings().strategy
    return ForecastStrategy(
        books, duration_h=fleet_duration_h([b.capacity_kwh for b in batteries], [b.power_kw for b in batteries]),
        efficiency=cfg.battery.efficiency, soc_min=cfg.battery.soc_min, soc_max=cfg.battery.soc_max,
        tick_seconds=cfg.sim.tick_seconds, horizon_hours=st.horizon_hours, max_age_hours=st.max_age_hours,
        min_points=st.min_points, charge_margin=st.charge_margin, reserve_spike_usd=st.reserve_spike_usd,
        reserve_soc=st.reserve_soc,
    )


def report(results: dict, windows: list[Window]) -> str:
    cols = [str(w.start) for w in windows]
    lines = ["| strategy | " + " | ".join(f"{c} ({w.days} d)" for c, w in zip(cols, windows))
             + " | total $/battery | vs bands | cycles |",
             "|---" * (len(cols) + 4) + "|"]
    base = None
    for name, per_window in results.items():
        cells, total, cycles = [], 0.0, 0.0
        for c in cols:
            r = per_window[c]
            usd = float(np.mean([v["usd_per_battery"] for v in r.values()]))
            cells.append(f"${usd:.2f}")
            total += usd
            cycles += float(np.mean([v["cycles"] for v in r.values()]))
        if name == "bands":
            base = total
        rel = f"{100 * (total / base - 1):+.0f}%" if base else "-"
        lines.append(f"| {name} | " + " | ".join(cells) + f" | ${total:.2f} | {rel} | {cycles:.0f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from src.forecast.settings import load_settings

    ap = argparse.ArgumentParser(prog="python -m src.forecast.evaluate", description=__doc__.split("\n")[0])
    ap.add_argument("--cache-dir", type=Path, default=load_settings().cache_dir)
    ap.add_argument("--model")
    ap.add_argument("--windows", nargs="+", default=list(DEFAULT_WINDOWS), help="START:DAYS, local dates")
    ap.add_argument("--batteries", type=int, default=100, help="split evenly over the two locations")
    ap.add_argument("--strategies", nargs="+",
                    default=["bands", "peak", "forecast", "forecast/naive", "forecast/oracle"])
    args = ap.parse_args(argv)
    windows = [Window.parse(w) for w in args.windows]
    results = evaluate(windows, args.cache_dir, args.batteries, args.strategies, args.model)
    print(report(results, windows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
