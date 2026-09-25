"""Postgres series backend (src/data/series_pg.py): the same API shape as the snapshot backend.

Runs in a throwaway schema with SYNTHETIC rows (made up for the test). Parity
with the real snapshot numbers is in tests/test_series_parity.py. Skips when
Postgres is not reachable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from src.data import series, series_pg
from src.feeds.replay import database_available
from src.ingest import parse, store
from src.ingest.testdb import scratch_schema

pytestmark = pytest.mark.postgres
UTC = timezone.utc
T0 = datetime(2025, 6, 1, 5, tzinfo=UTC)
A, B = datetime(2025, 1, 1, 6, tzinfo=UTC), datetime(2026, 1, 1, 6, tzinfo=UTC)


@pytest.fixture(scope="module")
def db():
    if not database_available():
        pytest.skip("Postgres not reachable: set DATABASE_URL (scripts/dev/services.sh up)")
    with scratch_schema() as conn:
        ts = pd.date_range(T0, periods=8, freq="15min", tz="UTC")
        store.upsert_prices(conn, pd.DataFrame({"ts": ts[::-1], "location": "LZ_A",  # SYNTHETIC, stored out of order
                                                "price_usd_mwh": np.arange(8.0)[::-1] * 2.5 - 3}), "RT", 2025)
        store.upsert_prices(conn, pd.DataFrame({"ts": ts[::4], "location": "LZ_A", "price_usd_mwh": [30.0, 31.0]}), "DA", 2025)
        hours = pd.date_range(T0, periods=2, freq="h", tz="UTC", name="ts")
        store.upsert_native_load(conn, pd.DataFrame({"ERCOT": [50000.5, 50100.25], "COAST": [1.0, 2.0]}, index=hours), 2025)
        long = pd.DataFrame({"ts": np.repeat(ts[:4], 3), "fuel": ["Gas", "WSL", "Wind"] * 4,
                             "gen_mw": [300.0, -5.0, 100.0] * 4, "settlement_type": "FINAL"})
        store.upsert_generation(conn, long, parse.fuel_shares(parse.fuel_mw_wide(long)), 2025)
        yield conn


@pytest.fixture
def snapshot(monkeypatch):
    monkeypatch.setenv("SERIES_BACKEND", "snapshot")


def assert_same_shape(pg, snap):
    assert type(pg) is type(snap)
    assert pg.index.dtype == snap.index.dtype == "datetime64[ns, UTC]"
    assert pg.index.name == snap.index.name
    if isinstance(pg, pd.Series):
        assert pg.dtype == snap.dtype == "float64"
    else:
        assert (pg.dtypes == "float64").all() and pg.columns.name == snap.columns.name


def test_rt_prices_sorted_half_open_float64(db, snapshot):
    s = series_pg.rt_prices("LZ_A", T0 + timedelta(minutes=15), T0 + timedelta(minutes=75), conn=db)
    assert list(s.index) == list(pd.date_range(T0 + timedelta(minutes=15), periods=4, freq="15min", tz="UTC"))
    assert s.tolist() == [-0.5, 2.0, 4.5, 7.0] and s.name == "LZ_A"
    assert_same_shape(s, series.rt_prices("LZ_HOUSTON", T0, T0 + timedelta(hours=1)))


def test_da_prices(db, snapshot):
    s = series_pg.da_prices("LZ_A", A, B, conn=db)
    assert s.tolist() == [30.0, 31.0] and s.name == "LZ_A"
    assert_same_shape(s, series.da_prices("LZ_HOUSTON", A, B))


def test_empty_results_equal_the_snapshot_backend_exactly(db, snapshot):
    pd.testing.assert_series_equal(series_pg.rt_prices("LZ_NOWHERE", A, B, conn=db), series.rt_prices("LZ_NOWHERE", A, B))
    pd.testing.assert_series_equal(series_pg.da_prices("LZ_A", T0, T0, conn=db), series.da_prices("LZ_A", T0, T0))
    empty = series_pg.fuel_mix(T0 - timedelta(days=30), T0 - timedelta(days=29), conn=db)
    ref = series.fuel_mix(T0 - timedelta(days=30), T0 - timedelta(days=30))
    pd.testing.assert_frame_equal(empty, ref)
    assert series_pg.system_load(B, B + timedelta(days=1), conn=db).name == "ERCOT"


def test_bounds_in_another_zone_mean_the_same_instants(db):
    chicago = ZoneInfo("America/Chicago")
    utc = series_pg.rt_prices("LZ_A", T0, T0 + timedelta(hours=2), conn=db)
    local = series_pg.rt_prices("LZ_A", T0.astimezone(chicago), (T0 + timedelta(hours=2)).astimezone(chicago), conn=db)
    pd.testing.assert_series_equal(utc, local)


def test_naive_bounds_raise_like_the_snapshot_backend(db, snapshot):
    naive = datetime(2025, 6, 1)
    with pytest.raises(TypeError):
        series.rt_prices("LZ_HOUSTON", naive, naive + timedelta(days=1))
    with pytest.raises(TypeError):
        series_pg.rt_prices("LZ_A", naive, naive + timedelta(days=1), conn=db)


def test_system_load(db, snapshot):
    s = series_pg.system_load(A, B, conn=db)
    assert s.tolist() == [50000.5, 50100.25] and s.index[0] == pd.Timestamp(T0)
    assert s.name == "ERCOT" and s.index.name == "Interval Start"
    assert_same_shape(s, series.system_load(A, B))


def test_fuel_mix_shares(db, snapshot):
    df = series_pg.fuel_mix(A, B, conn=db)
    assert list(df.columns) == ["gas", "wind"] and len(df) == 4  # WSL excluded; only fuels present
    assert df.iloc[0].tolist() == [0.75, 0.25]
    assert_same_shape(df, series.fuel_mix(A, B))
