"""The insights replay books exactly the same money as the pitch backtest (real 2025 prices, small fleet)."""
import pytest

from src.backtest import insights
from src.backtest.runner import backtest_settings, build_context, run_strategy


@pytest.fixture(scope="module")
def ctx():
    return build_context(2025, settings=backtest_settings(), fleet_size=40)


def test_insights_revenue_matches_the_backtest(ctx):
    record = run_strategy("peak", ctx, lag0=False)
    got = insights.measure("peak", ctx)
    assert got["per_battery_usd"] == pytest.approx(record["metrics"]["per_battery_usd_year"], abs=0.01)
    assert got["avg_sell_usd_mwh"] > got["avg_buy_usd_mwh"] > -50
    assert 0 <= got["charging_renewable_share"] <= 1 and got["peak_hours"] >= 80
