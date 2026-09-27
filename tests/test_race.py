"""The live race (src/sim/race.py): the shadow fleet earns, city by city, exactly what src.backtest earns for the
comparison strategy over the same window from the same start state, bit for bit, on real snapshot prices. It is
driven the way the coordinator drives it: its own Strategy per city through the live run's PriceWindow
(StrategyTargets: decision_tick, the live lag, the same trailing history), stepped with engine.deliver.

Also: availability overrides changing mid-run (a segmented backtest with the same masks), the plain-language
race_update messages, the Race block's keys (docs/API.md), and the per-tick cost at 20,000 batteries.
The forecast case uses SYNTHETIC forecast books (test doubles, not TimesFM): there are none in a local checkout.
"""
from __future__ import annotations

import dataclasses
import json
import re
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.backtest.engine import build_fleets, fill_ledger, load_ticks, replay_location
from src.backtest.runner import history_ticks, replay
from src.config import REPO_ROOT, load_config
from src.ledger.backtest import BacktestLedger
from src.sim import race as race_mod
from src.sim.coordinator import PriceWindow, StrategyTargets, price_feed, snapshot_years
from src.strategy import make_strategy

CFG = load_config()
LOCS = list(CFG.fleet.locations)
UTC = timezone.utc


def _start(start_utc: str) -> datetime:
    return datetime.fromisoformat(start_utc.replace("Z", "+00:00"))


def run_race(name, start_utc, ticks, soc0, size, *, strategies=None, changes=()):
    """The race as the coordinator runs it. changes: [(tick k, {location: offline mask})] applied from tick k."""
    cfg = CFG
    start = _start(start_utc)
    feed = price_feed(snapshot_years(start), LOCS)
    window = cfg.strategy.bands.window_days * cfg.sim.ticks_per_day
    prices = PriceWindow(feed, LOCS, int(start.timestamp()), lag=cfg.sim.decision_lag_ticks, window=window,
                         warmup_s=cfg.strategy.bands.window_days * 86400, tick_s=cfg.sim.tick_seconds)
    strategies = strategies or {loc: make_strategy(cfg.strategy, cfg.sim.tick_seconds, name) for loc in LOCS}
    targets = StrategyTargets(strategies, prices, cfg.sim.decision_lag_ticks, cfg.sim.tick_seconds)
    fleets = build_fleets(size, cfg.fleet.locations, cfg.battery, cfg.seed)
    race = race_mod.Race.start(name, name, fleets, soc0, dt_h=cfg.sim.dt_hours, soc_min=cfg.battery.soc_min,
                               soc_max=cfg.battery.soc_max)
    offline = {loc: np.zeros(f.size, dtype=bool) for loc, f in fleets.items()}
    changes = dict(changes)
    per_tick_ms = []
    for k in range(ticks):
        ts = int(start.timestamp()) + k * cfg.sim.tick_seconds
        prices.ensure(ts)
        offline = changes.get(k, offline)

        def decide(loc, fsoc, headroom, k=k, ts=ts):
            dec = targets(k, ts, loc, fsoc, headroom)
            return dec.raw_kw, dec.now

        race.step(decide, offline)
        per_tick_ms.append(race.last_ms)
    return race, per_tick_ms


def run_backtest(name, start_utc, ticks, soc0, size, *, strategies=None, changes=(), fast=True):
    """src.backtest over the same window from the same start state (warm-up = window_days before the start, as
    tests/test_fleet_backtest_parity.py builds it). With availability changes: one engine replay per segment,
    each starting from the SoC the previous one ended with."""
    cfg = CFG
    start = _start(start_utc)
    end = start + timedelta(seconds=ticks * cfg.sim.tick_seconds)
    feed = price_feed(snapshot_years(start), LOCS)
    series = load_ticks(feed, LOCS, start, end, start - timedelta(days=cfg.strategy.bands.window_days))
    fleets = build_fleets(size, cfg.fleet.locations, cfg.battery, cfg.seed, soc_init=soc0)
    strategies = strategies or {loc: make_strategy(cfg.strategy, cfg.sim.tick_seconds, name) for loc in LOCS}
    if not changes:
        bt = replay(name, cfg, series, fleets, lag=cfg.sim.decision_lag_ticks, fast=fast, strategies=strategies)
        return bt.summary, {loc: r.soc_final for loc, r in bt.runs.items()}
    bounds = [0, *[k for k, _ in changes], ticks]
    masks = [None, *[m for _, m in changes]]
    delivered = {loc: [] for loc in LOCS}
    soc = {loc: f.soc_init for loc, f in fleets.items()}
    for (a, b), mask in zip(zip(bounds, bounds[1:]), masks):
        for loc, ts in series.items():
            f = fleets[loc]
            avail = np.ones(f.size, dtype=bool) if mask is None else ~mask[loc]
            seg = dataclasses.replace(f, soc_init=soc[loc], available=avail)
            r = replay_location(ts.ticks[:ts.start + b], ts.start + a, seg, strategies[loc],
                                lag=cfg.sim.decision_lag_ticks, history_ticks=history_ticks(cfg),
                                dt_h=cfg.sim.dt_hours, soc_min=cfg.battery.soc_min, soc_max=cfg.battery.soc_max)
            delivered[loc].append(r.delivered_kwh)
            soc[loc] = r.soc_final
    ledger = BacktestLedger({loc: f.size for loc, f in fleets.items()})
    fill_ledger(ledger, [(series[loc].replayed, np.concatenate(delivered[loc])) for loc in LOCS])
    return ledger.summary(), soc


def assert_same(race, summary, soc_final):
    for loc in LOCS:
        c = race.cities[loc]
        assert c.revenue_usd == summary["per_location_usd"][loc], loc  # bit for bit, not approximately
        assert np.array_equal(c.soc, soc_final[loc]), loc
    assert race.revenue_usd() == summary["total_usd"]


@pytest.mark.parametrize("name, start_utc, ticks, soc0, size", [
    ("bands", "2025-07-11T23:00:00Z", 72, 0.9, 20000),  # the July 11 spike evening, the full fleet: 6 pm to midnight
    ("bands", "2025-01-01T06:00:00Z", 288, 0.5, 2000),  # warm-up reaches back into 2024
    ("bands", "2025-05-16T21:00:00Z", 96, 0.9, 2000),   # the spike evening
])
def test_the_race_earns_exactly_what_the_backtest_earns(name, start_utc, ticks, soc0, size):
    race, _ = run_race(name, start_utc, ticks, soc0, size)
    for fast in (True, False):  # the bands fast path and the per-tick seam
        summary, soc_final = run_backtest(name, start_utc, ticks, soc0, size, fast=fast)
        assert_same(race, summary, soc_final)
    assert all(abs(c.revenue_usd) > 0 for c in race.cities.values())  # every city really traded


def synthetic_books(start: datetime, hours: int):
    """SYNTHETIC forecast books (test doubles, not TimesFM): hourly issues, a fixed daily shape."""
    from src.forecast.provider import ForecastBook

    first = int(start.timestamp()) - 8 * 86400
    n = 8 * 24 + hours + 2
    issued = (first + np.arange(n, dtype=np.int64) * 3600) * 10**9
    hour = ((issued // 10**9 // 3600) % 24).astype(np.float32)
    shape = 40.0 + 30.0 * np.sin((hour[:, None] + np.arange(96, dtype=np.float32)[None, :] / 4) / 24 * 2 * np.pi)
    return {loc: ForecastBook(loc, "rt_price", "synthetic", 15, issued, issued.copy(), shape - 5, shape, shape + 5)
            for loc in LOCS}


def test_the_race_on_the_ai_forecast_is_the_backtest_too():
    from src.strategy.forecast import ForecastStrategy

    start_utc = "2025-05-16T21:00:00Z"
    books = synthetic_books(_start(start_utc), 12)
    strategies = {loc: ForecastStrategy({loc: books[loc]}, duration_h=2.0) for loc in LOCS}
    race, _ = run_race("forecast", start_utc, 96, 0.6, 2000, strategies=strategies)
    summary, soc_final = run_backtest("forecast", start_utc, 96, 0.6, 2000, strategies=strategies)
    assert_same(race, summary, soc_final)
    assert sum(abs(c.revenue_usd) for c in race.cities.values()) > 0


def test_availability_overrides_mid_run_are_mirrored_exactly():
    """30% of Houston and a block of Dallas-Fort Worth go offline at 8 pm and come back at 9 pm, while the simple
    rule sells (2025-07-11, the spike evening from 6 pm): the race equals a backtest replayed in segments
    with those very masks, and an offline battery holds its charge while it is offline."""
    size = 2000
    fleets = build_fleets(size, CFG.fleet.locations, CFG.battery, CFG.seed)
    rng = np.random.default_rng(7)
    off = {loc: np.zeros(f.size, dtype=bool) for loc, f in fleets.items()}
    off["LZ_HOUSTON"][rng.permutation(fleets["LZ_HOUSTON"].size)[:int(0.3 * fleets["LZ_HOUSTON"].size)]] = True
    off["LZ_NORTH"][100:400] = True  # a controller's contiguous block
    back = {loc: np.zeros(f.size, dtype=bool) for loc, f in fleets.items()}
    changes = [(24, off), (36, back)]
    start_utc = "2025-07-11T23:00:00Z"
    before, _ = run_race("bands", start_utc, 24, 0.9, size)
    during, _ = run_race("bands", start_utc, 36, 0.9, size, changes=changes[:1])
    for loc in ("LZ_HOUSTON", "LZ_NORTH"):  # offline from tick 24 to 35: no movement at all; the others sold
        held = off[loc]
        assert np.array_equal(during.cities[loc].soc[held], before.cities[loc].soc[held])
        assert (during.cities[loc].soc[~held] < before.cities[loc].soc[~held]).all()
    race, _ = run_race("bands", start_utc, 72, 0.9, size, changes=changes)
    summary, soc_final = run_backtest("bands", start_utc, 72, 0.9, size, changes=changes)
    assert_same(race, summary, soc_final)
    plain, _ = run_race("bands", start_utc, 72, 0.9, size)
    assert plain.revenue_usd() != race.revenue_usd()  # the overrides really changed what happened


def test_the_race_block_is_the_contract():
    text = (REPO_ROOT / "docs" / "API.md").read_text()
    example = json.loads(re.search(r"\*\*Race\*\*.*?```json\s*(\{.*?\})\s*```", text, re.S).group(1))
    race, _ = run_race("bands", "2025-07-11T23:00:00Z", 12, 0.9, 400)
    block = race.public(live_revenue_usd=1000.0)
    assert set(block) == set(example) and list(block["by_location"]) == LOCS
    assert all(set(v) == set(example["by_location"]["LZ_HOUSTON"]) for v in block["by_location"].values())
    assert block["live_minus_race_usd"] == pytest.approx(1000.0 - race.revenue_usd(), abs=1e-6)
    assert block["revenue_usd"] == pytest.approx(sum(v["revenue_usd"] for v in block["by_location"].values()), abs=1e-5)
    assert {v["action"] for v in block["by_location"].values()} <= {"sell", "charge", "hold"}
    caps = {loc: float(c.fleet.capacity_kwh.sum()) for loc, c in race.cities.items()}
    level = sum(block["by_location"][loc]["charge_level"] * caps[loc] for loc in LOCS) / sum(caps.values())
    assert block["charge_level"] == pytest.approx(level, abs=1e-4)
    json.dumps(block, allow_nan=False)
    assert race_mod.comparison_strategy("forecast") == "bands" and race_mod.comparison_strategy("peak") == "bands"
    assert race_mod.comparison_strategy("bands") == "forecast"


def test_race_update_messages():
    shares = {"LZ_HOUSTON": 7000, "LZ_NORTH": 8000, "LZ_CPS": 2600, "LZ_AEN": 2400}
    jul11 = {"LZ_HOUSTON": 1754.5, "LZ_NORTH": 1757.3, "LZ_CPS": 1759.9, "LZ_AEN": 1757.4}  # 2025-07-11 9 pm
    # docs/API.md's examples, word for word
    assert race_mod.spike_started_message(jul11, 4, "forecast", 0.54, "bands", 0.21, shares) == (
        "Prices jumped to $1,757 in all four cities. The AI fleet had 54% charge left; the simple rule had 21%.")
    assert race_mod.spike_ended_message("forecast", "bands", 12400.0) == "The AI is now $12,400 ahead of the simple rule."
    # the AI is named first even when it is the race and the live fleet runs the simple rule
    assert race_mod.spike_started_message(jul11, 4, "bands", 0.21, "forecast", 0.54, shares) == (
        "Prices jumped to $1,757 in all four cities. The AI fleet had 54% charge left; the simple rule had 21%.")
    assert race_mod.spike_ended_message("bands", "forecast", -12400.0) == (
        "The AI is now $12,400 ahead of the simple rule.")
    assert race_mod.spike_ended_message("forecast", "bands", -3100.4) == "The AI is now $3,100 behind the simple rule."
    assert race_mod.spike_ended_message("forecast", "bands", 0.3) == "The AI and the simple rule have earned the same so far."
    assert race_mod.spike_started_message({"LZ_HOUSTON": 2137.4}, 4, "peak", 0.64, "bands", 0.24) == (
        "The price in Houston jumped to $2,137. The evening-peak rule had 64% charge left; the simple rule had 24%.")
    may16 = {"LZ_NORTH": 1005.2, "LZ_CPS": 1295.1, "LZ_AEN": 1120.0}  # far apart: the highest
    assert race_mod.spike_started_message(may16, 4, "peak", 0.5, "bands", 0.3, shares) == (
        "Prices jumped to as much as $1,295 in Dallas–Fort Worth, San Antonio and Austin. The evening-peak rule had "
        "50% charge left; the simple rule had 30%.")
    assert race_mod.spike_ended_message("peak", "bands", 2500.0) == ("The evening-peak rule is now $2,500 ahead of "
                                                                     "the simple rule.")
    assert race_mod.subject_lead("bands", "forecast", -5.0) == 5.0 and race_mod.subject_lead("peak", "bands", 5.0) == 5.0


def test_per_tick_cost_at_20000_batteries(capsys):
    """The race's whole step (4 cities: strategy decision + allocation + physics + metering) at 20,000 batteries.
    It runs between the command dispatch and the telemetry barrier; the barrier's deadline is 250 ms."""
    from src.strategy.forecast import ForecastStrategy

    start_utc = "2025-07-11T23:00:00Z"
    _, bands_ms = run_race("bands", start_utc, 72, 0.9, 20000)
    books = synthetic_books(_start(start_utc), 8)
    strategies = {loc: ForecastStrategy({loc: books[loc]}, duration_h=2.0) for loc in LOCS}
    _, ai_ms = run_race("forecast", start_utc, 72, 0.9, 20000, strategies=strategies)
    with capsys.disabled():
        for label, ms in (("bands", bands_ms), ("forecast", ai_ms)):
            print(f"\n[race] {label} at 20,000 batteries: per tick p50 {np.percentile(ms, 50):.3f} ms, "
                  f"p99 {np.percentile(ms, 99):.3f} ms, max {max(ms):.3f} ms")
    assert np.percentile(bands_ms, 50) < 25 and np.percentile(ai_ms, 50) < 25  # vs the 250 ms deadline
