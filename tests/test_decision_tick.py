"""The strategy sees the decision-time clock with the last *published* price (RUNLOG decision 5)."""
from datetime import datetime, timedelta, timezone

from src.config import load_config
from src.core.models import PriceTick
from src.sim import skeleton
from src.sim.decision import decision_tick

UTC = timezone.utc


def test_decision_tick_takes_clock_from_now_and_price_from_published():
    now = PriceTick(datetime(2025, 5, 17, 0, 0, tzinfo=UTC), "LZ_HOUSTON", 2250.0, None, None)
    published = PriceTick(now.ts - timedelta(minutes=15), "LZ_HOUSTON", 61.5, 55000.0, {"gas": 0.5})
    seen = decision_tick(now, published)
    assert seen.ts == now.ts and seen.location == "LZ_HOUSTON"
    assert seen.price_usd_mwh == 61.5 and seen.demand_mw == 55000.0 and seen.fuel_mix == {"gas": 0.5}


def test_peak_windows_stay_on_the_clock_under_decision_lag():
    cfg = load_config()
    lag0 = skeleton.run(cfg, strategy_name="peak", lag=0)
    lag3 = skeleton.run(cfg, strategy_name="peak", lag=3)
    assert lag3.summary["total_usd"] == lag0.summary["total_usd"]
