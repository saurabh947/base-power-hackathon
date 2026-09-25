"""Behaviour of the in-memory implementations beyond the frozen seams (sim-only extras, input checks)."""

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.core.ids import make_command_id
from src.core.models import BatteryState, Command, PriceTick
from src.feeds.memory import InMemoryPriceFeed, forward_fill
from src.sim.adapters import InMemoryBatteryAdapter

UTC = timezone.utc
T0 = datetime(2025, 5, 16, 5, 0, tzinfo=UTC)
FIVE = timedelta(minutes=5)


def cmd(setpoint, tick, bid="a", command_id=None):
    ts = T0 + tick * FIVE
    return Command(command_id or make_command_id("r", bid, ts), bid, setpoint, ts)


def adapter(**kw):
    return InMemoryBatteryAdapter([BatteryState("a", 13.5, 5.0, 0.5, 0.9, kw.get("available", True))])


# --- adapter ------------------------------------------------------------------------


def test_counters_separate_applied_duplicate_and_stale():
    a = adapter()
    a.apply(cmd(1.0, 1))
    a.apply(cmd(1.0, 1))  # exact replay
    a.apply(cmd(1.0, 0))  # older tick
    a.apply(cmd(1.0, 1, command_id="other"))  # same tick, other id
    a.apply(cmd(1.0, 2))
    assert a.counters == {"applied": 2, "duplicate": 1, "stale": 2}


def test_command_consumed_while_offline_is_not_replayed_when_back_online():
    a = adapter(available=False)
    c = cmd(5.0, 0)
    a.apply(c)
    a.set_available("a", True)
    a.apply(c)  # replay after coming back: fenced, moves nothing
    assert a.read("a").soc == 0.5 and a.read("a").available is True
    a.apply(cmd(5.0, 1))
    assert a.read("a").soc < 0.5


def test_adapter_rejects_bad_construction():
    ok = BatteryState("a", 13.5, 5.0, 0.5, 0.9, True)
    with pytest.raises(ValueError):
        InMemoryBatteryAdapter([ok, ok])  # duplicate id
    for bad in (BatteryState("a", 0.0, 5.0, 0.5, 0.9, True), BatteryState("a", 13.5, 5.0, 0.5, 1.5, True),
                BatteryState("a", 13.5, 5.0, math.nan, 0.9, True)):
        with pytest.raises(ValueError):
            InMemoryBatteryAdapter([bad])
    assert InMemoryBatteryAdapter([ok]).battery_ids == ["a"]


# --- feed ---------------------------------------------------------------------------


def row(ts, price=30.0, loc="LZ_HOUSTON"):
    return PriceTick(ts, loc, price, None, None)


def test_forward_fill_makes_three_ticks_per_interval():
    ticks = forward_fill([row(T0, 10.0), row(T0 + timedelta(minutes=15), 20.0)])
    assert [(t.ts - T0, t.price_usd_mwh) for t in ticks] == [
        (timedelta(minutes=m), p) for m, p in ((0, 10.0), (5, 10.0), (10, 10.0), (15, 20.0), (20, 20.0), (25, 20.0))
    ]


@pytest.mark.parametrize("bad_rows", [
    [row(T0 + timedelta(minutes=5))],  # not on the 15-minute grid
    [row(T0.replace(tzinfo=None))],  # naive
])
def test_forward_fill_rejects_bad_intervals(bad_rows):
    with pytest.raises(ValueError):
        forward_fill(bad_rows)


@pytest.mark.parametrize("bad_ticks", [
    [row(T0), row(T0)],  # duplicate tick
    [row(T0, math.nan)],  # non-finite price
    [row(T0 + timedelta(minutes=1))],  # off the 5-minute grid
])
def test_feed_rejects_bad_ticks(bad_ticks):
    with pytest.raises(ValueError):
        InMemoryPriceFeed(bad_ticks)


def test_feed_normalises_input_zones_to_utc():
    local = T0.astimezone(ZoneInfo("America/Chicago"))
    feed = InMemoryPriceFeed([row(local)])
    (t,) = feed.ticks(T0, T0 + FIVE, "LZ_HOUSTON")
    assert t.ts == T0 and t.ts.tzinfo is UTC
