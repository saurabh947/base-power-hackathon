"""2026 through the real coordinator and worker processes (Redis; small fleet): real prices up to where they end,
then the AI's prediction. Real prices are the committed 2026 snapshot (published 2026-09-20: real until
2026-09-20 05:00Z). The predictions are SYNTHETIC paths (test doubles written where the prediction job writes them;
no TimesFM here), so every predicted price can be checked exactly.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.config import load_config
from src.data import series, years
from src.forecast import predictions
from src.sim import control as ctl
from src.sim import coordinator as coord
from src.sim.coordinator import Coordinator
from src.sim.redis_layout import reachable, redis_url

pytestmark = pytest.mark.redis
UTC = timezone.utc
LOCS = list(load_config().fleet.locations)
CUTOFF = pd.Timestamp("2026-09-20T05:00Z")  # the committed snapshot's real_until_utc (midnight CDT)


def synthetic_path(loc: str, k: int, n: int = 4 * 96) -> predictions.PredictionPath:
    """SYNTHETIC p50: a daily wave, 20-80 $/MWh, peaking in the evening; each city offset by k dollars."""
    ts = CUTOFF.value + np.arange(n, dtype=np.int64) * 15 * 60 * 10**9
    hour = (pd.to_datetime(ts, utc=True).tz_convert("America/Chicago").hour.to_numpy() + 0.0)
    p50 = np.round(50.0 + k + 30.0 * np.sin((hour - 13.0) / 24.0 * 2 * np.pi), 2)
    return predictions.PredictionPath(loc, "timesfm-3.0", CUTOFF, pd.Timestamp("2026-09-26T12:00Z"), ts,
                                      np.round(p50 - 8, 2), p50, np.round(p50 + 8, 2),
                                      (np.arange(n) // 672).astype(np.int16))


@pytest.fixture
def co(tmp_path, monkeypatch):
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    from src.forecast import settings as fsettings

    real_settings = fsettings.load_settings
    monkeypatch.delenv("PRICE_SOURCE", raising=False)  # real prices: the snapshot
    monkeypatch.setenv("FORECAST_SOURCE", "parquet")
    monkeypatch.setattr(predictions, "default_cache_dir", lambda: tmp_path / "predictions")
    monkeypatch.setattr(fsettings, "load_settings",  # no real forecasts: only the virtual ones after the cutoff
                        lambda *a, **kw: real_settings(*a, **kw).with_overrides(cache_dir=tmp_path / "forecasts"))
    monkeypatch.setattr(coord, "_STRATEGY_CACHE", {})
    years.clear_cache()
    predictions.clear_cache()
    predictions.write([synthetic_path(loc, k) for k, loc in enumerate(LOCS)], out="parquet",
                      cache_dir=tmp_path / "predictions")
    c = Coordinator(url=url, prefix=f"test:pred:{uuid.uuid4().hex[:8]}:", fleet_size=400, workers=4)
    c.boot()
    yield c
    c.shutdown()
    c.cleanup_keys()
    years.clear_cache()
    predictions.clear_cache()


def run_all(c):
    assert c.serve_until(lambda: c.run.status == "finished", timeout_s=120)
    return [r for r in c.tick_log]


def test_real_prices_until_they_end_then_the_prediction(co):
    co.start({"preset": "real_prices_end_2026", "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=96)
    assert co.run.settings["year"] == 2026 and co.run.settings["start_utc"] == "2026-09-19T23:00:00Z"
    assert co.run.cutoff == int(CUTOFF.timestamp()) and co.run.predicted_locations == tuple(LOCS)
    log = run_all(co)
    assert len(log) == 96
    real = {loc: series.rt_prices(loc, datetime(2026, 9, 19, 23, tzinfo=UTC), CUTOFF.to_pydatetime()) for loc in LOCS}
    paths = {loc: synthetic_path(loc, k) for k, loc in enumerate(LOCS)}
    for r in log:
        ts = pd.Timestamp(r["ts_utc"])
        assert r["predicted"] is (ts >= CUTOFF)  # docs/API.md Tick.predicted
        for loc in LOCS:
            if ts < CUTOFF:  # real ERCOT: the interval's price, held for its three ticks
                assert r["price"][loc] == round(float(real[loc][ts.floor("15min")]), 4), (ts, loc)
            else:  # the prediction's p50, held for its three ticks
                i = int((ts.floor("15min") - CUTOFF) / pd.Timedelta(minutes=15))
                assert r["price"][loc] == paths[loc].p50[i], (ts, loc)
    assert sum(1 for r in log if not r["predicted"]) == 72  # 6 pm to midnight: 36 s at 2 ticks/s
    # prediction_started: once, at the first predicted tick, in the contract's words
    started = [e for e in co.events if e["kind"] == "prediction_started"]
    assert len(started) == 1 and started[0]["sim_utc"] == "2026-09-20T05:00:00Z"
    assert started[0]["message"] == ctl.PREDICTION_STARTED_MESSAGE == (
        "From here on, prices are the AI's prediction (TimesFM-3), not real ERCOT data.")
    assert started[0]["detail"]["real_until_utc"] == "2026-09-20T05:00:00Z"
    assert started[0]["detail"]["locations"] == LOCS and started[0]["detail"]["model"] == "timesfm-3.0"
    run_started = next(e for e in co.events if e["kind"] == "run_started")
    assert run_started["message"].endswith("; real prices end at September 20, 2026, 12:00 am, and the AI's "
                                           "prediction takes over from there.")
    # the history the API serves carries the flag too
    stored = [r for r in co.r.xrange(co.keys.ticks)]
    import json

    flags = [json.loads(f[b"j"])["predicted"] for _, f in stored]
    assert flags.count(False) == 72 and flags.count(True) == 24
    # the evening peak traded on the predicted prices (it sells 6-9 pm: before the cutoff here), and the race ran
    assert co.run.race is not None and co.run.race.ticks == 96 and co.run.potential.ticks == 96


def test_the_ai_acts_on_virtual_forecasts_after_the_cutoff(co):
    """No real forecasts at all here: after the cutoff the AI strategy runs on the hourly slices of the prediction
    (its own long-range prediction), so it has something to act on without looking at anything real."""
    co.start({"start_utc": CUTOFF.isoformat().replace("+00:00", "Z"), "speed_tps": "max", "strategy": "forecast",
              "start_charge_level": 0.5}, max_ticks=288, internal=True)
    assert co.run.strategy_name == "forecast" and not co.run.strategy_fallback  # every city runs the AI
    book = co.run.decide.strategies["LZ_HOUSTON"].books["LZ_HOUSTON"]
    issued = pd.to_datetime(book.issued, utc=True)
    assert issued[0] == CUTOFF and (np.diff(book.issued) == 3600 * 10**9).all()  # hourly virtual forecasts
    path = synthetic_path("LZ_HOUSTON", 0)
    assert np.array_equal(book.p50[5], path.p50[20:116].astype(np.float32))  # issued 5 h in: the next 24 h
    log = run_all(co)
    assert all(r["predicted"] for r in log) and len(log) == 288
    actions = [r["action"]["LZ_HOUSTON"] for r in log]
    assert "sell" in actions and "charge" in actions  # it trades the predicted daily wave
    first_prediction = [e for e in co.events if e["kind"] == "prediction_started"]
    assert len(first_prediction) == 1 and first_prediction[0]["sim_utc"] == "2026-09-20T05:00:00Z"  # tick 0


def test_without_a_prediction_the_replay_ends_where_real_prices_end(co, tmp_path, monkeypatch):
    monkeypatch.setattr(predictions, "default_cache_dir", lambda: tmp_path / "none")
    predictions.clear_cache()
    co.start({"start_utc": "2026-09-19T23:00:00Z", "speed_tps": "max", "strategy": "peak"}, internal=True)
    assert co.run.cutoff == int(CUTOFF.timestamp()) and co.run.predicted_locations == ()
    log = run_all(co)
    assert len(log) == 72 and not any(r["predicted"] for r in log)
    finished = next(e for e in co.events if e["kind"] == "run_finished")
    assert "the end of the real prices" in finished["message"]
    assert finished["message"].endswith("the AI's prediction for the rest of the year isn't ready yet.")
    assert not [e for e in co.events if e["kind"] == "prediction_started"]


def test_2026_from_january_1_warms_up_on_real_december_2025_prices(co):
    co.start({"preset": "jan1_2026", "speed_tps": "max", "strategy": "bands"}, max_ticks=3)
    prices = co.run.prices
    first = prices._ts["LZ_HOUSTON"][0]
    assert datetime.fromtimestamp(int(first), UTC) == datetime(2025, 12, 25, 6, tzinfo=UTC)  # 7 days before
    now, published, history = prices.decision_inputs("LZ_HOUSTON", int(datetime(2026, 1, 1, 6, tzinfo=UTC)
                                                                         .timestamp()), co.lag)
    assert len(history) == 2016 - 3 and published.ts < datetime(2026, 1, 1, 6, tzinfo=UTC)
    real = series.rt_prices("LZ_HOUSTON", datetime(2025, 12, 25, 6, tzinfo=UTC), datetime(2026, 1, 1, 6, tzinfo=UTC))
    assert history[:3] == [float(real.iloc[0])] * 3  # real 2025 prices fill the bands window
    log = run_all(co)
    assert len(log) == 3 and not any(r["predicted"] for r in log)
    assert log[0]["ts_utc"] == "2026-01-01T06:00:00Z"
    assert timedelta(0) < CUTOFF - pd.Timestamp(log[-1]["ts_utc"])
