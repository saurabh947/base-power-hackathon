"""M0 acceptance: the walking skeleton runs end to end on real prices and earns nonzero USD."""

import math
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import load_config
from src.sim import m0_prices
from src.sim.skeleton import main, run

CHICAGO = ZoneInfo("America/Chicago")


# --- the pasted real prices -----------------------------------------------------------


def test_m0_prices_are_the_real_extract():
    day, warm = m0_prices.PRICES_2025_05_16, m0_prices.WARMUP_2025_05_09_TO_15
    assert (len(day), len(warm)) == (96, 672)
    for p in day + warm:  # ERCOT SPPs: finite, 2 decimals
        assert math.isfinite(p) and round(p, 2) == p
    # Checksums from the one-off extraction guard against accidental edits.
    assert round(sum(day), 2) == 13310.64 and round(sum(warm), 2) == 22050.52
    assert max(day) == 2250.15 and day.index(2250.15) == 79  # 19:45 CDT
    assert (min(day), min(warm), max(warm)) == (18.28, 0.9, 179.2)


def test_m0_interval_rows_are_contiguous_local_days():
    rows = m0_prices.interval_rows()
    assert len(rows) == 8 * 96
    assert all(b.ts - a.ts == timedelta(minutes=15) for a, b in zip(rows, rows[1:]))
    assert rows[0].ts.astimezone(CHICAGO).isoformat() == "2025-05-09T00:00:00-05:00"
    assert rows[672].ts == m0_prices.DAY_START_UTC
    assert rows[-1].ts.astimezone(CHICAGO).isoformat() == "2025-05-16T23:45:00-05:00"


# --- the tick loop ------------------------------------------------------------------


@pytest.fixture(scope="module")
def result():
    return run()


def test_accept_summary_returns_nonzero_usd(result):
    s = result.summary
    assert set(s) == {"total_usd", "per_location_usd", "per_battery_avg_usd", "sold_kwh", "bought_kwh",
                      "top_spike_days"}
    assert s["total_usd"] != 0.0
    assert s["per_battery_avg_usd"] == s["total_usd"]  # one battery
    assert s["per_location_usd"] == {"LZ_HOUSTON": s["total_usd"]}
    assert [d["date"] for d in s["top_spike_days"]] == ["2025-05-16"]


def test_one_row_per_5min_tick_of_the_local_day(result):
    assert len(result.rows) == 288
    assert result.rows[0].ts == m0_prices.DAY_START_UTC
    assert all(r.ts.astimezone(CHICAGO).date().isoformat() == "2025-05-16" for r in result.rows)


def test_strategy_never_peeks(result):
    lag = load_config().sim.decision_lag_ticks
    by_ts = {r.ts: r.price_usd_mwh for r in result.rows}
    for k, r in enumerate(result.rows):
        seen_ts = r.ts - lag * timedelta(minutes=5)
        if seen_ts in by_ts:
            assert r.seen_price_usd_mwh == by_ts[seen_ts]
    # the first ticks of the day see the published end of 2025-05-15
    assert result.rows[0].seen_price_usd_mwh == m0_prices.WARMUP_2025_05_09_TO_15[-1]


def test_every_tick_respects_the_physics(result):
    b = result.start
    dt_h = 5 / 60
    for r in result.rows:
        assert abs(r.delivered_kwh) <= b.power_kw * dt_h + 1e-12
        assert 0.2 - 1e-12 <= r.soc <= 1.0 + 1e-12
        assert r.delivered_kwh == 0.0 or math.copysign(1, r.delivered_kwh) == math.copysign(1, r.target_kw)


def test_ledger_matches_the_metered_energy_and_storage_balances(result):
    s, b = result.summary, result.start
    rows = result.rows
    assert s["total_usd"] == pytest.approx(sum(r.delivered_kwh * r.price_usd_mwh / 1000 for r in rows), abs=1e-12)
    sold = sum(r.delivered_kwh for r in rows if r.delivered_kwh > 0)
    bought = -sum(r.delivered_kwh for r in rows if r.delivered_kwh < 0)
    assert (s["sold_kwh"], s["bought_kwh"]) == (pytest.approx(sold), pytest.approx(bought))
    # storage change = bought * sqrt(eta) - sold / sqrt(eta)
    stored = (result.final.soc - b.soc) * b.capacity_kwh
    assert stored == pytest.approx(bought * math.sqrt(b.efficiency) - sold / math.sqrt(b.efficiency), abs=1e-9)


def test_runs_are_deterministic(result):
    assert run().summary == result.summary


def test_peak_swaps_in_through_config_and_also_earns():
    peak = run(strategy_name="peak", lag=0)
    assert peak.summary["total_usd"] != 0.0
    charging = {r.ts.astimezone(CHICAGO).hour for r in peak.rows if r.delivered_kwh < 0}
    selling = {r.ts.astimezone(CHICAGO).hour for r in peak.rows if r.delivered_kwh > 0}
    assert charging <= set(range(10, 15)) and selling <= set(range(18, 21))


def test_main_prints_the_summary_and_exits_zero(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Ledger.summary() =" in out and "-> PASS" in out
