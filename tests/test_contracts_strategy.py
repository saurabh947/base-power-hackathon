"""Contract tests for the Strategy seam (spec sections 4 and 8, RUNLOG decisions).

GENERIC tests run against every factory in FACTORIES. A future solver or
forecast strategy plugs in by adding a factory. Every strategy must be pure,
return a finite float in [-H, +H], and return 0 when H == 0.
Strategies that need price history also go in HISTORY_FACTORIES and must hold
(return 0) while the history is empty.

Then come the rules of the two v1 strategies:
  bands: trailing 7-day window (2016 ticks) excluding the current tick; hold
         with fewer than 288 samples; charge if price <= p25; else discharge
         if price >= p90 and price > 0; else hold.
  peak:  charge 10:00-15:00, discharge 18:00-21:00 America/Chicago, half-open
         windows, correct across DST, ignores prices.
"""

import copy
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from src.core.models import PriceTick
from src.strategy.bands import BandsStrategy
from src.strategy.peak import PeakStrategy

UTC = timezone.utc
CHICAGO = ZoneInfo("America/Chicago")
FIVE = timedelta(minutes=5)
H = 1000.0


def _bands():
    return BandsStrategy()


def _peak():
    return PeakStrategy()


FACTORIES = [pytest.param(_bands, id="bands"), pytest.param(_peak, id="peak")]
HISTORY_FACTORIES = [pytest.param(_bands, id="bands")]


def tick(price: float, ts: datetime = datetime(2025, 5, 16, 23, 0, tzinfo=UTC), loc="LZ_HOUSTON"):
    return PriceTick(ts, loc, price, None, None)


def local_tick(y, mo, d, hh, mm, price=30.0):
    return tick(price, datetime(y, mo, d, hh, mm, tzinfo=CHICAGO).astimezone(UTC))


def random_cases(n=400, seed=20250516):
    """Random but realistic inputs: ERCOT-like prices with spikes and negatives."""
    rng = np.random.default_rng(seed)
    year_start = datetime(2025, 1, 1, 6, 0, tzinfo=UTC)
    for _ in range(n):
        size = int(rng.choice([0, 5, 287, 288, 289, 2016, 2500]))
        hist = rng.normal(40.0, 25.0, size)
        spikes = rng.random(size) < 0.02
        hist[spikes] = rng.uniform(500.0, 5000.0, spikes.sum())
        negatives = rng.random(size) < 0.03
        hist[negatives] = rng.uniform(-50.0, 0.0, negatives.sum())
        price = float(rng.choice([rng.normal(40.0, 25.0), -20.0, 0.0, 5000.0, 9999.0,
                                  float(np.percentile(hist, 25)) if size else 1.0]))
        headroom = float(rng.choice([0.0, 5.0, 1e3, 1.15e5, rng.uniform(0.0, 2.3e5)]))
        ts = year_start + int(rng.integers(0, 105120)) * FIVE
        soc = float(rng.uniform(0.2, 1.0))
        yield tick(round(price, 2), ts), soc, headroom, [round(float(x), 2) for x in hist]


# --- generic contract: every strategy ---------------------------------------------


@pytest.mark.parametrize("make", FACTORIES)
def test_output_is_a_finite_float_within_plus_minus_headroom(make):
    strategy = make()
    for t, soc, headroom, history in random_cases():
        out = strategy.target_kw(t, soc, headroom, history)
        assert isinstance(out, float)
        assert math.isfinite(out)
        assert -headroom <= out <= headroom


@pytest.mark.parametrize("make", FACTORIES)
def test_zero_headroom_means_zero(make):
    strategy = make()
    for t, soc, _, history in random_cases(n=100):
        assert strategy.target_kw(t, soc, 0.0, history) == 0.0


@pytest.mark.parametrize("make", FACTORIES)
def test_pure_deterministic_and_does_not_mutate_inputs(make):
    strategy = make()
    cases = list(random_cases(n=50, seed=7))
    first = []
    for t, soc, headroom, history in cases:
        before = copy.deepcopy(history)
        first.append(strategy.target_kw(t, soc, headroom, history))
        assert history == before
    fresh = make()
    for (t, soc, headroom, history), expected in zip(reversed(cases), reversed(first)):
        assert strategy.target_kw(t, soc, headroom, history) == expected  # order-independent
        assert fresh.target_kw(t, soc, headroom, history) == expected  # no hidden state


@pytest.mark.parametrize("make", HISTORY_FACTORIES)
def test_history_strategies_hold_without_history(make):
    strategy = make()
    for price in (-500.0, 0.0, 25.0, 5000.0):
        assert strategy.target_kw(tick(price), 0.5, H, []) == 0.0


# --- bands (strategy v1) ---------------------------------------------------------


def test_bands_holds_while_history_is_short():
    strategy = BandsStrategy()
    for n in (1, 100, 287):
        history = [float(i) for i in range(1, n + 1)]
        for price in (-500.0, 0.0, 5000.0):
            assert strategy.target_kw(tick(price), 0.5, H, history) == 0.0


# 288 samples whose percentiles are exact (numpy 'linear'): sorted, p25 sits
# between two 10.0s and p90 between two 30.0s, so p25 = 10.0 and p90 = 30.0.
BANDED = [10.0] * 100 + [20.0] * 100 + [30.0] * 88


@pytest.mark.parametrize("price, expected", [
    (-40.0, -H),   # far below p25
    (10.0, -H),    # at p25: charge (inclusive)
    (10.01, 0.0),  # just above p25
    (20.0, 0.0),   # inside the band: hold
    (29.99, 0.0),  # just below p90
    (30.0, H),     # at p90: discharge (inclusive)
    (5000.0, H),   # spike
])
def test_bands_thresholds_hand_computed(price, expected):
    assert BandsStrategy().target_kw(tick(price), 0.5, H, BANDED) == expected


def test_bands_is_active_at_exactly_288_samples():
    assert len(BANDED) == 288
    assert BandsStrategy().target_kw(tick(30.0), 0.5, H, BANDED) == H
    assert BandsStrategy().target_kw(tick(30.0), 0.5, H, BANDED[1:]) == 0.0


def test_bands_discharge_needs_a_positive_price():
    negative = [-30.0] * 100 + [-20.0] * 100 + [-10.0] * 88  # p25 = -30, p90 = -10
    strategy = BandsStrategy()
    assert strategy.target_kw(tick(-10.0), 0.5, H, negative) == 0.0  # >= p90 but not > 0
    assert strategy.target_kw(tick(0.0), 0.5, H, negative) == 0.0
    assert strategy.target_kw(tick(0.01), 0.5, H, negative) == H
    assert strategy.target_kw(tick(-30.0), 0.5, H, negative) == -H  # <= p25 still charges


def test_bands_flat_window_tie_goes_to_charge():
    # p25 == p90: the rules are checked in spec order, charge first.
    assert BandsStrategy().target_kw(tick(20.0), 0.5, H, [20.0] * 288) == -H


def test_bands_window_is_the_trailing_7_days_only():
    recent = [10.0] * 1008 + [30.0] * 1008  # exactly 7 days: p25 = 10, p90 = 30
    strategy = BandsStrategy()
    # Old spikes would lift p90 to 5000 if they leaked into the window.
    assert strategy.target_kw(tick(30.0), 0.5, H, [5000.0] * 1000 + recent) == H
    # Old troughs would drop p25 to -1000 if they leaked into the window.
    assert strategy.target_kw(tick(10.0), 0.5, H, [-1000.0] * 1000 + recent) == -H


def test_bands_uses_history_as_given_and_not_the_current_price():
    # The caller passes history strictly before the tick. If the strategy
    # appended the current price, p90 of [1..288, 259.3] would exceed 259.3.
    history = [float(i) for i in range(1, 289)]
    p90 = float(np.percentile(history, 90))
    assert BandsStrategy().target_kw(tick(p90), 0.5, H, history) == H


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_bands_holds_on_a_non_finite_price(bad):
    assert BandsStrategy().target_kw(tick(bad), 0.5, H, BANDED) == 0.0


# --- peak (the swap-proof strategy) -----------------------------------------------


@pytest.mark.parametrize("hh, mm, expected", [
    (0, 0, 0.0), (9, 55, 0.0),
    (10, 0, -H), (12, 30, -H), (14, 55, -H),  # charge window [10:00, 15:00)
    (15, 0, 0.0), (17, 55, 0.0),
    (18, 0, H), (19, 45, H), (20, 55, H),     # discharge window [18:00, 21:00)
    (21, 0, 0.0), (23, 55, 0.0),
])
def test_peak_windows_are_half_open_in_local_time(hh, mm, expected):
    for day in (date(2025, 1, 15), date(2025, 7, 15)):  # CST (UTC-6) and CDT (UTC-5)
        t = local_tick(day.year, day.month, day.day, hh, mm)
        assert PeakStrategy().target_kw(t, 0.5, H, []) == expected


@pytest.mark.parametrize("utc_ts, expected", [
    # 2025-03-09 spring forward: the day runs on CDT (UTC-5) by 10:00.
    (datetime(2025, 3, 9, 14, 55, tzinfo=UTC), 0.0),   # 09:55 CDT
    (datetime(2025, 3, 9, 15, 0, tzinfo=UTC), -H),     # 10:00 CDT (a fixed-CST bug says 09:00)
    (datetime(2025, 3, 9, 23, 0, tzinfo=UTC), H),      # 18:00 CDT
    (datetime(2025, 3, 10, 2, 0, tzinfo=UTC), 0.0),    # 21:00 CDT
    # 2025-11-02 fall back: the day runs on CST (UTC-6) by 10:00.
    (datetime(2025, 11, 2, 15, 0, tzinfo=UTC), 0.0),   # 09:00 CST (a fixed-CDT bug says 10:00)
    (datetime(2025, 11, 2, 16, 0, tzinfo=UTC), -H),    # 10:00 CST
    (datetime(2025, 11, 3, 0, 0, tzinfo=UTC), H),      # 18:00 CST
    (datetime(2025, 11, 3, 2, 55, tzinfo=UTC), H),     # 20:55 CST
    (datetime(2025, 11, 3, 3, 0, tzinfo=UTC), 0.0),    # 21:00 CST
])
def test_peak_is_correct_on_dst_change_days(utc_ts, expected):
    assert PeakStrategy().target_kw(tick(30.0, utc_ts), 0.5, H, []) == expected


def test_peak_every_local_day_of_2025_has_5h_charge_and_3h_discharge():
    strategy = PeakStrategy()
    counts: dict[date, list[int]] = {}
    ts = datetime(2025, 1, 1, 6, 0, tzinfo=UTC)  # local midnight, 2025-01-01 CST
    end = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    while ts < end:
        local_day = ts.astimezone(CHICAGO).date()
        out = strategy.target_kw(tick(30.0, ts), 0.5, H, [])
        c = counts.setdefault(local_day, [0, 0, 0])
        c[0] += 1
        c[1] += out < 0
        c[2] += out > 0
        ts += FIVE
    assert len(counts) == 365
    assert counts[date(2025, 3, 9)][0] == 276  # 23-hour day
    assert counts[date(2025, 11, 2)][0] == 300  # 25-hour day
    for day, (_, charge, discharge) in counts.items():
        assert (charge, discharge) == (60, 36), day


def test_peak_ignores_prices_and_history():
    strategy = PeakStrategy()
    t_charge = datetime(2025, 7, 15, 15, 0, tzinfo=UTC)  # 10:00 CDT
    t_idle = datetime(2025, 7, 15, 13, 0, tzinfo=UTC)  # 08:00 CDT
    for price, history in ((-50.0, []), (5000.0, BANDED), (30.0, [9999.0] * 3000)):
        assert strategy.target_kw(tick(price, t_charge), 0.5, H, history) == -H
        assert strategy.target_kw(tick(price, t_idle), 0.5, H, history) == 0.0
