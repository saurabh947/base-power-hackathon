"""Accuracy metrics (hand-checked on SYNTHETIC numbers) and the forecast config section."""
import numpy as np
import pandas as pd
import pytest

from src.config import ConfigError, load_raw_config
from src.forecast import accuracy, store
from src.forecast.inputs import Grid
from src.forecast.provider import ForecastBook
from src.forecast.settings import load_settings


def test_pinball_by_hand():
    # y = 10 vs (p10, p50, p90) = (5, 10, 20): 0.1*5 = 0.5, 0, (1-0.9)*10 = 1.0 -> mean 0.5
    assert accuracy.pinball(np.array([10.0]), np.array([[5.0, 10.0, 20.0]]))[0] == pytest.approx(0.5)
    # a point forecast scores half its absolute error
    assert accuracy.pinball(np.array([10.0]), np.array([[4.0, 4.0, 4.0]]))[0] == pytest.approx(3.0)


def test_seasonal_naive_uses_only_ended_intervals():
    """SYNTHETIC actuals: value i = i on a 15-min grid, so each baseline names the index it used."""
    t0 = pd.Timestamp("2025-05-01T05:00Z")
    grid = Grid.from_series(pd.Series(np.arange(96 * 12, dtype=float),
                                      index=pd.date_range(t0, periods=96 * 12, freq="15min")), 15)
    issued = t0 + pd.Timedelta(days=9, minutes=15 * 7)  # 7 quarter-hours into day 9
    p50 = np.full(96, 5.0, dtype=np.float32)
    row = store.ForecastRow(issued, "X", "rt_price", "m", issued, 15, p50, p50, p50)
    book = ForecastBook.from_frame(store._frame([row]), "X", "rt_price", "m")
    s = accuracy.score(book, grid)
    first = 9 * 96 + 7
    assert s.y.tolist() == list(range(first, first + 96))
    assert s.sn1.tolist() == [i - 96 for i in range(first, first + 96)]  # same time yesterday, all ended
    assert (s.sn1 < first).all()
    assert s.sn7[:, 1].tolist() == [i - 96 * 4 for i in range(first, first + 96)]  # median of days -1..-7
    m = accuracy.metrics(s)
    assert m["sn1_mae"] == pytest.approx(96.0) and m["sn1_pinball"] == pytest.approx(48.0)


def test_demand_naive_reaches_back_two_days_for_hours_24_to_48():
    t0 = pd.Timestamp("2025-05-01T05:00Z")
    grid = Grid.from_series(pd.Series(np.arange(24 * 12, dtype=float),
                                      index=pd.date_range(t0, periods=24 * 12, freq="h")), 60)
    issued = t0 + pd.Timedelta(days=9)
    p = np.zeros(48, dtype=np.float32)
    book = ForecastBook.from_frame(store._frame([store.ForecastRow(issued, "ERCOT", "demand", "m", issued, 60, p, p, p)]),
                                   "ERCOT", "demand", "m")
    s = accuracy.score(book, grid)
    first = 9 * 24
    assert s.sn1[:24].tolist() == [i - 24 for i in range(first, first + 24)]
    assert s.sn1[24:].tolist() == [i - 48 for i in range(first + 24, first + 48)]


def test_settings_from_config():
    s = load_settings(env={})
    assert s.model == "timesfm-3.0"
    assert (s.rt_price.step_minutes, s.rt_price.horizon_steps, s.rt_price.every_minutes) == (15, 96, 60)
    assert s.rt_price.horizon_minutes == 24 * 60
    assert (s.demand.step_minutes, s.demand.horizon_steps) == (60, 48)
    assert s.demand_location == "ERCOT" and s.locations == ("LZ_HOUSTON", "LZ_NORTH")
    assert s.weights_dir.name == "models"


def test_model_switch_by_env():
    assert load_settings(env={"FORECAST_MODEL": "timesfm-2.5"}).model == "timesfm-2.5"
    with pytest.raises(ConfigError):
        load_settings(env={"FORECAST_MODEL": "timesfm-9"})


@pytest.mark.parametrize("path, value", [
    (("model",), "chronos"),
    (("rt_price", "covariates"), "weather"),
    (("demand", "covariates"), "da"),
    (("rt_price", "every_minutes"), 20),  # not a multiple of 15
    (("strategy", "horizon_hours"), 0),
    (("strategy", "reserve_soc"), 1.5),
])
def test_bad_values_raise(path, value):
    raw = load_raw_config()["forecast"]
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ConfigError):
        load_settings(raw, env={})
