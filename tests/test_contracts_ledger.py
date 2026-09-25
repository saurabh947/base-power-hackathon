"""Contract tests for the Ledger seam (spec sections 4 and 9, RUNLOG decisions).

Every Ledger implementation must pass this suite. To plug one in (e.g. one
that persists to backtest_runs, or real billing later), add a factory to
FACTORIES. A factory takes the fleet size per location, which the ledger
uses for the per-battery average.

Sign convention: delivered_kwh is grid-side, + = sold, - = bought.
usd = delivered_kwh * price_usd_mwh / 1000.
"""

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from src.core.interfaces import Ledger
from src.core.models import PriceTick
from src.ledger.backtest import BacktestLedger

UTC = timezone.utc
KEYS = {"total_usd", "per_location_usd", "per_battery_avg_usd", "sold_kwh", "bought_kwh", "top_spike_days"}
FLEET = {"LZ_HOUSTON": 2, "LZ_NORTH": 2}


def _backtest(fleet_size_by_location: dict[str, int]) -> Ledger:
    return BacktestLedger(fleet_size_by_location)


FACTORIES = [pytest.param(_backtest, id="backtest")]


@pytest.fixture(params=FACTORIES)
def make_ledger(request):
    return request.param


def tick(ts: datetime, price: float, loc: str = "LZ_HOUSTON") -> PriceTick:
    return PriceTick(ts, loc, price, None, None)


T = datetime(2025, 5, 16, 23, 0, tzinfo=UTC)  # 18:00 CDT


def test_summary_has_exactly_the_fixed_keys(make_ledger):
    ledger = make_ledger(FLEET)
    empty = ledger.summary()
    assert set(empty) == KEYS
    assert empty["total_usd"] == 0.0
    assert empty["per_location_usd"] == {"LZ_HOUSTON": 0.0, "LZ_NORTH": 0.0}
    assert empty["per_battery_avg_usd"] == 0.0
    assert empty["sold_kwh"] == 0.0 and empty["bought_kwh"] == 0.0
    assert empty["top_spike_days"] == []
    ledger.record(tick(T, 50.0), 1.0)
    assert set(ledger.summary()) == KEYS


def test_selling_1_kwh_at_5000_per_mwh_earns_5_dollars(make_ledger):
    ledger = make_ledger(FLEET)
    ledger.record(tick(T, 5000.0), 1.0)
    s = ledger.summary()
    assert s["total_usd"] == pytest.approx(5.00, abs=1e-12)
    assert s["per_location_usd"]["LZ_HOUSTON"] == pytest.approx(5.00, abs=1e-12)
    assert s["sold_kwh"] == pytest.approx(1.0) and s["bought_kwh"] == 0.0


def test_buying_at_a_negative_price_earns_money(make_ledger):
    ledger = make_ledger(FLEET)
    ledger.record(tick(T, -20.0), -2.0)  # paid $20/MWh to take 2 kWh
    s = ledger.summary()
    assert s["total_usd"] == pytest.approx(+0.04, abs=1e-12)
    assert s["bought_kwh"] == pytest.approx(2.0) and s["sold_kwh"] == 0.0


def test_buying_at_a_positive_price_costs_money(make_ledger):
    ledger = make_ledger(FLEET)
    ledger.record(tick(T, 30.0), -1.0)
    assert ledger.summary()["total_usd"] == pytest.approx(-0.03, abs=1e-12)


def test_selling_at_a_negative_price_costs_money(make_ledger):
    ledger = make_ledger(FLEET)
    ledger.record(tick(T, -10.0), 1.0)
    assert ledger.summary()["total_usd"] == pytest.approx(-0.01, abs=1e-12)


def test_hand_computed_multi_day_multi_location_period(make_ledger):
    ledger = make_ledger(FLEET)  # 4 batteries in total
    rows = [
        # UTC ts,                                  loc,         $/MWh,   kWh    ->  USD       local date (CDT)
        (datetime(2025, 5, 16, 15, 0, tzinfo=UTC), "LZ_HOUSTON", 25.00, -4.0),  # -0.10     05-16 10:00
        (datetime(2025, 5, 17, 0, 45, tzinfo=UTC), "LZ_HOUSTON", 2250.15, 3.0),  # +6.75045  05-16 19:45
        (datetime(2025, 5, 17, 3, 0, tzinfo=UTC), "LZ_NORTH", -5.00, -2.0),  # +0.01     05-16 22:00
        (datetime(2025, 5, 17, 18, 0, tzinfo=UTC), "LZ_NORTH", 100.00, 1.5),  # +0.15     05-17 13:00
        (datetime(2025, 5, 18, 1, 0, tzinfo=UTC), "LZ_HOUSTON", 1000.00, 0.5),  # +0.50     05-17 20:00
    ]
    for ts, loc, price, kwh in rows:
        ledger.record(tick(ts, price, loc), kwh)
    s = ledger.summary()
    assert s["total_usd"] == pytest.approx(7.31045, abs=1e-9)
    assert s["per_location_usd"] == pytest.approx({"LZ_HOUSTON": 7.15045, "LZ_NORTH": 0.16}, abs=1e-9)
    assert s["per_battery_avg_usd"] == pytest.approx(7.31045 / 4, abs=1e-9)
    assert s["sold_kwh"] == pytest.approx(5.0, abs=1e-12)
    assert s["bought_kwh"] == pytest.approx(6.0, abs=1e-12)
    # Days are America/Chicago dates: 03:00 UTC on 05-17 still belongs to 05-16.
    days = s["top_spike_days"]
    assert [d["date"] for d in days] == ["2025-05-16", "2025-05-17"]
    assert days[0]["usd"] == pytest.approx(6.66045, abs=1e-9)
    assert days[1]["usd"] == pytest.approx(0.65, abs=1e-9)


def test_top_spike_days_are_the_10_best_highest_first(make_ledger):
    ledger = make_ledger(FLEET)
    start = datetime(2025, 7, 1, 23, 0, tzinfo=UTC)  # 18:00 CDT each day
    for d in range(15):
        ledger.record(tick(start + timedelta(days=d), 100.0 * (d + 1)), 1.0)  # day d earns $0.1*(d+1)
    days = ledger.summary()["top_spike_days"]
    assert len(days) == 10
    assert [d["date"] for d in days] == [f"2025-07-{day:02d}" for day in range(15, 5, -1)]
    assert [d["usd"] for d in days] == pytest.approx([0.1 * n for n in range(15, 5, -1)], abs=1e-12)
    assert all(set(d) == {"date", "usd"} for d in days)


def test_records_are_additive(make_ledger):
    split = make_ledger(FLEET)
    split.record(tick(T, 80.0), 1.0)
    split.record(tick(T, 80.0), 2.0)  # e.g. two workers reporting the same tick
    whole = make_ledger(FLEET)
    whole.record(tick(T, 80.0), 3.0)
    a, b = split.summary(), whole.summary()
    assert a["total_usd"] == pytest.approx(b["total_usd"], abs=1e-12)
    assert a["sold_kwh"] == pytest.approx(b["sold_kwh"], abs=1e-12)
    assert a["top_spike_days"][0]["usd"] == pytest.approx(b["top_spike_days"][0]["usd"], abs=1e-12)


def test_zero_kwh_changes_no_totals(make_ledger):
    ledger = make_ledger(FLEET)
    ledger.record(tick(T, 5000.0), 0.0)
    s = ledger.summary()
    assert s["total_usd"] == 0.0 and s["sold_kwh"] == 0.0 and s["bought_kwh"] == 0.0


@pytest.mark.parametrize("kwh, price", [(math.nan, 50.0), (math.inf, 50.0), (-math.inf, 50.0),
                                        (1.0, math.nan), (1.0, math.inf)])
def test_non_finite_values_are_rejected_without_side_effects(make_ledger, kwh, price):
    ledger = make_ledger(FLEET)
    ledger.record(tick(T, 50.0), 1.0)
    before = ledger.summary()
    with pytest.raises(ValueError):
        ledger.record(tick(T, price), kwh)
    assert ledger.summary() == before


def test_summary_is_json_serializable_and_a_copy(make_ledger):
    ledger = make_ledger(FLEET)
    ledger.record(tick(T, 5000.0), 1.0)
    s = ledger.summary()
    json.dumps(s)  # persisted to backtest_runs and exported as JSON later
    s["per_location_usd"]["LZ_HOUSTON"] = 999.0
    s["top_spike_days"].clear()
    again = ledger.summary()
    assert again["per_location_usd"]["LZ_HOUSTON"] == pytest.approx(5.0)
    assert len(again["top_spike_days"]) == 1
