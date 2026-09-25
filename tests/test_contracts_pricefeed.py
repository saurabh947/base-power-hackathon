"""Contract tests for the PriceFeed seam (spec section 4, RUNLOG decisions).

Every PriceFeed implementation must pass this suite. To plug one in (e.g. the
Postgres replay feed in M1), add a factory to FACTORIES. A factory takes
15-min NATIVE interval rows, exactly as stored in the prices table
(PriceTick with ts = interval start in UTC, rows in any order), and returns a
PriceFeed over them. The feed must yield the forward-filled 5-min ticks.
"""

import math
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.core.interfaces import PriceFeed
from src.core.models import PriceTick
from src.feeds.memory import InMemoryPriceFeed

UTC = timezone.utc
CHICAGO = ZoneInfo("America/Chicago")
FIVE = timedelta(minutes=5)
FIFTEEN = timedelta(minutes=15)

# 24 intervals (6 h) spanning the 2025-11-02 fall-back, when local 01:00-02:00
# happens twice. UTC must still be strictly increasing.
T0 = datetime(2025, 11, 2, 4, 0, tzinfo=UTC)  # 2025-11-01 23:00 CDT
N_INTERVALS = 24
T_END = T0 + N_INTERVALS * FIFTEEN
GAP = T0 + 10 * FIFTEEN  # this Houston interval is missing from the source
PRICES = [round(18.5 + 3.25 * i, 2) for i in range(N_INTERVALS)]
PRICES[5] = -12.34  # negative prices are real and must pass through
PRICES[17] = 5000.0  # so are spikes
DEMAND = 51234.5
FUEL_MIX = {"gas": 0.41, "wind": 0.32, "solar": 0.0, "nuclear": 0.1, "coal": 0.17}


def _memory(rows: list[PriceTick]) -> PriceFeed:
    return InMemoryPriceFeed.from_intervals(rows)


FACTORIES = [pytest.param(_memory, id="memory")]


@pytest.fixture(params=FACTORIES)
def feed(request) -> PriceFeed:
    return request.param(_source_rows())


def _source_rows() -> list[PriceTick]:
    rows = []
    for i in range(N_INTERVALS):
        ts = T0 + i * FIFTEEN
        if ts != GAP:
            rows.append(PriceTick(ts, "LZ_HOUSTON", PRICES[i], DEMAND, FUEL_MIX))
        rows.append(PriceTick(ts, "LZ_NORTH", PRICES[i] + 1000.0, None, None))
    random.Random(7).shuffle(rows)  # storage order is not replay order
    return rows


def _houston_intervals() -> list[tuple[datetime, float]]:
    return [(T0 + i * FIFTEEN, PRICES[i]) for i in range(N_INTERVALS) if T0 + i * FIFTEEN != GAP]


def test_yields_only_the_requested_location(feed):
    houston = list(feed.ticks(T0, T_END, "LZ_HOUSTON"))
    north = list(feed.ticks(T0, T_END, "LZ_NORTH"))
    assert houston and all(t.location == "LZ_HOUSTON" for t in houston)
    assert north and all(t.location == "LZ_NORTH" for t in north)
    north_by_ts = {t.ts: t.price_usd_mwh for t in north}
    for t in houston:  # no cross-location leakage of prices
        assert north_by_ts[t.ts] == pytest.approx(t.price_usd_mwh + 1000.0)


def test_ts_is_tz_aware_utc_strictly_increasing_on_the_5min_grid(feed):
    ticks = list(feed.ticks(T0, T_END, "LZ_NORTH"))
    assert len(ticks) == 3 * N_INTERVALS
    for t in ticks:
        assert t.ts.tzinfo is not None and t.ts.utcoffset() == timedelta(0)
        assert t.ts.minute % 5 == 0 and t.ts.second == 0 and t.ts.microsecond == 0
    assert all(a.ts < b.ts for a, b in zip(ticks, ticks[1:]))
    assert all(b.ts - a.ts == FIVE for a, b in zip(ticks, ticks[1:]))  # no gap in LZ_NORTH


def test_range_is_half_open(feed):
    start, end = T0 + 4 * FIFTEEN, T0 + 8 * FIFTEEN  # one hour, clear of the gap
    ticks = list(feed.ticks(start, end, "LZ_HOUSTON"))
    assert [t.ts for t in ticks] == [start + k * FIVE for k in range(12)]
    assert ticks[0].ts == start  # start included
    assert all(t.ts < end for t in ticks)  # end excluded


def test_bounds_inside_an_interval(feed):
    interval = T0 + 3 * FIFTEEN
    ticks = list(feed.ticks(interval + FIVE, interval + 2 * FIVE, "LZ_HOUSTON"))
    assert [t.ts for t in ticks] == [interval + FIVE]
    assert ticks[0].price_usd_mwh == PRICES[3]


def test_forward_fill_stays_inside_each_interval(feed):
    ticks = list(feed.ticks(T0, T_END, "LZ_HOUSTON"))
    by_ts = {t.ts: t.price_usd_mwh for t in ticks}
    intervals = _houston_intervals()
    assert len(ticks) == 3 * len(intervals)
    for start, price in intervals:
        for k in range(3):
            assert by_ts[start + k * FIVE] == price
    for k in range(3):  # the missing interval stays missing: never filled across a gap
        assert GAP + k * FIVE not in by_ts


def test_prices_are_finite_floats_and_context_passes_through(feed):
    ticks = list(feed.ticks(T0, T_END, "LZ_HOUSTON"))
    assert {t.price_usd_mwh for t in ticks} == {p for _, p in _houston_intervals()}
    for t in ticks:
        assert isinstance(t.price_usd_mwh, float) and math.isfinite(t.price_usd_mwh)
        assert t.demand_mw == DEMAND
        assert t.fuel_mix == FUEL_MIX


def test_empty_results(feed):
    assert list(feed.ticks(T0, T0, "LZ_HOUSTON")) == []
    assert list(feed.ticks(T_END, T0, "LZ_HOUSTON")) == []
    assert list(feed.ticks(T0, T_END, "LZ_WEST")) == []
    assert list(feed.ticks(T0 - timedelta(days=1), T0, "LZ_HOUSTON")) == []
    assert list(feed.ticks(T_END, T_END + timedelta(days=1), "LZ_HOUSTON")) == []


def test_naive_bounds_are_rejected(feed):
    naive_start = T0.replace(tzinfo=None)
    naive_end = T_END.replace(tzinfo=None)
    with pytest.raises(ValueError):
        list(feed.ticks(naive_start, T_END, "LZ_HOUSTON"))
    with pytest.raises(ValueError):
        list(feed.ticks(T0, naive_end, "LZ_HOUSTON"))


def test_bounds_in_another_zone_mean_the_same_instants(feed):
    utc = list(feed.ticks(T0 + FIFTEEN, T_END - FIFTEEN, "LZ_HOUSTON"))
    local = list(feed.ticks((T0 + FIFTEEN).astimezone(CHICAGO),
                            (T_END - FIFTEEN).astimezone(CHICAGO), "LZ_HOUSTON"))
    assert [(t.ts, t.price_usd_mwh) for t in local] == [(t.ts, t.price_usd_mwh) for t in utc]
    assert all(t.ts.utcoffset() == timedelta(0) for t in local)  # output stays UTC


def test_returns_an_iterator_and_replays_deterministically(feed):
    it = feed.ticks(T0, T_END, "LZ_HOUSTON")
    assert iter(it) is it
    first = list(it)
    again = list(feed.ticks(T0, T_END, "LZ_HOUSTON"))
    assert [(t.ts, t.price_usd_mwh) for t in first] == [(t.ts, t.price_usd_mwh) for t in again]
