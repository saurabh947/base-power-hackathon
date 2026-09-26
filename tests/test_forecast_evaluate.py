"""The small-fleet evaluation harness, on real snapshot prices (no model needed)."""
from datetime import date

import numpy as np
import pytest

from src.config import load_config
from src.forecast.evaluate import Prices, Window, reference_book, simulate
from src.sim.fleet import generate_batteries
from src.strategy import make_strategy
from src.strategy.forecast import ForecastStrategy


@pytest.fixture(autouse=True)
def snapshot_backend(monkeypatch):
    monkeypatch.setenv("SERIES_BACKEND", "snapshot")


@pytest.fixture(scope="module")
def may16():
    start, end = Window(date(2025, 5, 16), 1).bounds()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SERIES_BACKEND", "snapshot")
        return Prices.load("LZ_HOUSTON", start, end), start, end


def test_harness_matches_the_m0_skeleton(may16):
    """Same battery, day, lag and physics as `python -m src.sim.skeleton --strategy peak` ($4.4470, RUNLOG)."""
    prices, start, end = may16
    cfg = load_config()
    battery = generate_batteries(1, cfg.battery, cfg.seed)
    r = simulate(make_strategy(cfg.strategy, cfg.sim.tick_seconds, "peak"), prices, start, end, battery, cfg)
    assert r["usd_per_battery"] == pytest.approx(4.4470, abs=5e-5)
    assert r["actions"]["sell"] == 36 and r["actions"]["buy"] == 60


def test_reference_books_peek_or_look_back_exactly_one_day(may16):
    prices, start, end = may16
    g = prices.grid
    first = g.ended_before(start)
    oracle = reference_book(prices, start, end, "oracle")
    naive = reference_book(prices, start, end, "naive")
    assert len(oracle) == len(naive) == 24 and oracle.horizon == 96
    assert np.array_equal(oracle.p50[0], g.values[first:first + 96])
    assert np.array_equal(naive.p50[0], g.values[first - 96:first])  # all ended by the issue time
    assert oracle.issued[0] == start.value


def test_oracle_forecast_beats_the_naive_one(may16):
    prices, start, end = may16
    cfg = load_config()
    batteries = generate_batteries(4, cfg.battery, cfg.seed)
    usd = {}
    for kind in ("naive", "oracle"):
        book = reference_book(prices, start, end, kind)
        strategy = ForecastStrategy({"LZ_HOUSTON": book}, duration_h=1.8)
        usd[kind] = simulate(strategy, prices, start, end, batteries, cfg)["usd_per_battery"]
    assert usd["oracle"] > usd["naive"]
    assert all(np.isfinite(v) for v in usd.values())
