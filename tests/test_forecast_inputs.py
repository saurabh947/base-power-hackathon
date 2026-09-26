"""No look-ahead: a forecast issued at T sees only intervals that ENDED at or before T.

Grids built from SYNTHETIC values (labelled) test the slicing rules; the last
tests use the real committed ERCOT snapshot.
"""
from datetime import time, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.forecast import inputs
from src.forecast.inputs import Grid

UTC = timezone.utc
T0 = pd.Timestamp("2025-05-10T05:00:00Z")


def synthetic_grid(n=2000, step=15):
    """SYNTHETIC: value i = i, so a context's values name the intervals it used."""
    idx = pd.date_range(T0, periods=n, freq=f"{step}min")
    return Grid.from_series(pd.Series(np.arange(n, dtype=float), index=idx), step)


def test_context_ends_with_the_last_interval_that_ended():
    g = synthetic_grid()
    t = T0 + pd.Timedelta(minutes=15 * 100)  # interval 99 ends exactly at t
    ctx = g.context(t, 10)
    assert list(ctx) == list(range(90, 100))
    assert g.first_open(t) == t


def test_context_mid_interval_excludes_the_open_interval():
    g = synthetic_grid()
    t = T0 + pd.Timedelta(minutes=15 * 100 + 7)  # interval 100 is still open
    assert g.context(t, 3).tolist() == [97.0, 98.0, 99.0]
    assert g.first_open(t) == T0 + pd.Timedelta(minutes=15 * 100)


def test_hourly_grid_waits_for_the_whole_hour():
    g = synthetic_grid(step=60)
    t = T0 + pd.Timedelta(hours=10, minutes=59)
    assert g.context(t, 2).tolist() == [8.0, 9.0]  # hour 10 has not ended
    assert g.context(t + pd.Timedelta(minutes=1), 2).tolist() == [9.0, 10.0]


def test_changing_the_future_never_changes_the_context():
    g = synthetic_grid()
    rng = np.random.default_rng(0)
    for _ in range(50):
        t = T0 + pd.Timedelta(minutes=int(rng.integers(0, 15 * 1900)))
        before = g.context(t, 512).copy()
        future = g.ended_before(t)
        poisoned = Grid(g.t0, g.step_minutes, g.values.copy())
        poisoned.values[future:] = 1e9  # SYNTHETIC poison in every interval not yet ended
        assert np.array_equal(poisoned.context(t, 512), before)
        assert (before < 1e9).all()


def test_short_history_gives_a_shorter_context():
    g = synthetic_grid()
    assert len(g.context(T0 + pd.Timedelta(minutes=45), 512)) == 3
    assert len(g.context(T0, 512)) == 0
    assert len(g.context(T0 - pd.Timedelta(days=1), 512)) == 0


def test_gaps_stay_missing_as_nan():
    idx = pd.date_range(T0, periods=10, freq="15min").delete([4, 5])
    g = Grid.from_series(pd.Series(np.arange(8, dtype=float), index=idx), 15)
    assert len(g.values) == 10 and np.isnan(g.values[[4, 5]]).all()


def test_grid_rejects_off_grid_or_duplicate_stamps():
    with pytest.raises(ValueError):
        Grid.from_series(pd.Series([1.0, 2.0], index=pd.DatetimeIndex([T0, T0 + pd.Timedelta(minutes=7)])), 15)
    with pytest.raises(ValueError):
        Grid.from_series(pd.Series([1.0, 2.0], index=pd.DatetimeIndex([T0, T0])), 15)


def test_issue_times_are_on_the_hour_and_half_open():
    ts = inputs.issue_times(pd.Timestamp("2025-01-01T06:00Z"), pd.Timestamp("2026-01-01T06:00Z"), 60)
    assert len(ts) == 8760 and ts[0] == pd.Timestamp("2025-01-01T06:00Z")
    assert ts[-1] == pd.Timestamp("2026-01-01T05:00Z")
    assert len(inputs.issue_times(pd.Timestamp("2025-01-01T06:10Z"), pd.Timestamp("2025-01-01T09:00Z"), 60)) == 2


# --- day-ahead prices: known future, but only once published ---------------------------


@pytest.mark.parametrize("local, known_until_local", [
    ("2025-05-16T13:29", "2025-05-17T00:00"),  # before 13:30: only today's DA is out
    ("2025-05-16T13:30", "2025-05-18T00:00"),  # from 13:30: tomorrow's too
    ("2025-05-16T00:00", "2025-05-17T00:00"),
    ("2025-03-08T14:00", "2025-03-10T00:00"),  # spans the spring-forward day
    ("2025-11-01T23:00", "2025-11-03T00:00"),  # spans the fall-back day
])
def test_da_known_until(local, known_until_local):
    tz = "America/Chicago"
    t = pd.Timestamp(local).tz_localize(tz)
    assert inputs.da_known_until(t, time(13, 30), tz) == pd.Timestamp(known_until_local).tz_localize(tz)


def test_da_covariate_uses_yesterdays_da_for_unpublished_hours():
    g = synthetic_grid(n=96 * 10)
    # SYNTHETIC DA: hour index h -> 1000 + h, on the RT grid
    hours = pd.date_range(T0.floor("h") - pd.Timedelta(days=2), periods=24 * 14, freq="h")
    da = inputs.da_on_grid(pd.Series(1000.0 + np.arange(len(hours)), index=hours), g)
    t = pd.Timestamp("2025-05-15T09:00", tz="America/Chicago").tz_convert("UTC")  # 09:00 CT: D+1 not out
    cov = inputs.da_covariate(da, g, t, 96, 96)
    assert cov.shape == (1, 192)
    end = g.ended_before(t)
    known = inputs.da_known_until(t)
    for j in range(96):
        i = end + j
        expected = da[i] if g.ts(i) < known else da[i - 96]
        assert cov[0, 96 + j] == expected
    assert (cov[0, 96:][[g.ts(end + j) >= known for j in range(96)]] < da[end:end + 96].max()).all()


def test_da_covariate_ignores_unpublished_values():
    g = synthetic_grid(n=96 * 10)
    hours = pd.date_range(T0.floor("h") - pd.Timedelta(days=2), periods=24 * 14, freq="h")
    da = inputs.da_on_grid(pd.Series(50.0 + np.arange(len(hours)) % 24, index=hours), g)
    t = pd.Timestamp("2025-05-15T10:00", tz="America/Chicago").tz_convert("UTC")
    base = inputs.da_covariate(da, g, t, 200, 96)
    known = g.ended_before(inputs.da_known_until(t))
    poisoned = da.copy()
    poisoned[known:] = -9999.0  # SYNTHETIC: DA prices not yet published at t
    assert np.array_equal(inputs.da_covariate(poisoned, g, t, 200, 96), base)


# --- real snapshot -------------------------------------------------------------------------


def test_real_context_matches_the_snapshot(monkeypatch):
    monkeypatch.setenv("SERIES_BACKEND", "snapshot")
    from src.data import series

    t = pd.Timestamp("2025-05-16T21:00Z")
    raw = series.rt_prices("LZ_HOUSTON", t - timedelta(days=12), t + timedelta(days=1))
    g = Grid.from_series(raw, 15)
    ctx = g.context(t, 512)
    assert len(ctx) == 512 and np.isfinite(ctx).all()
    expected = raw[raw.index < t].iloc[-512:].to_numpy(np.float32)
    assert np.array_equal(ctx, expected)
    assert ctx[-1] == np.float32(raw[pd.Timestamp("2025-05-16T20:45Z")])  # ended at 21:00, published by then
