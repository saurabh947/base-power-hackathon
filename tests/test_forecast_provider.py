"""Forecast storage and lookup: parquet cache, Postgres table, ForecastBook, latest_forecast().

Forecast values here are SYNTHETIC (labelled): the tests check plumbing, not accuracy.
"""
import os
import uuid

import numpy as np
import pandas as pd
import pytest

from src.forecast import provider, store
from src.forecast.provider import ForecastBook, latest_forecast
from src.forecast.store import ForecastRow

T = pd.Timestamp("2025-05-16T20:00Z")
MODEL = "timesfm-3.0"


def synthetic_row(issued, location="LZ_HOUSTON", series="rt_price", model=MODEL, level=40.0, horizon=96, step=15):
    """SYNTHETIC forecast: p50 = level + i, p10/p90 = p50 -/+ 10."""
    p50 = (level + np.arange(horizon)).astype(np.float32)
    return ForecastRow(pd.Timestamp(issued), location, series, model, pd.Timestamp(issued), step,
                       p50 - 10, p50, p50 + 10)


@pytest.fixture
def cache(tmp_path):
    sink = store.ParquetSink(tmp_path)
    sink.write([synthetic_row(T + pd.Timedelta(hours=h), level=10.0 * h) for h in range(3)])
    sink.write([synthetic_row(T, location="LZ_NORTH")])
    sink.write([synthetic_row(T, location="ERCOT", series="demand", level=50000.0, horizon=48, step=60)])
    provider.clear_cache()
    yield tmp_path
    provider.clear_cache()


def test_parquet_roundtrip_and_existing(cache):
    df = store.read_parquet(cache, MODEL, "rt_price", "LZ_HOUSTON")
    assert list(df["issued_at"]) == [T + pd.Timedelta(hours=h) for h in range(3)]
    assert df["p50"].iloc[1][0] == pytest.approx(10.0)
    got = store.ParquetSink(cache).existing(MODEL, "rt_price", "LZ_HOUSTON", T, T + pd.Timedelta(hours=2))
    assert got == {T, T + pd.Timedelta(hours=1)}


def test_parquet_last_write_wins(cache):
    store.ParquetSink(cache).write([synthetic_row(T + pd.Timedelta(hours=1), level=999.0)])
    book = ForecastBook.load("LZ_HOUSTON", model=MODEL, source="parquet", cache_dir=cache)
    assert len(book) == 3
    assert book.p50[1][0] == pytest.approx(999.0)


def test_book_latest_is_issued_at_or_before():
    book = ForecastBook.from_frame(store._frame([synthetic_row(T), synthetic_row(T + pd.Timedelta(hours=1))]),
                                   "LZ_HOUSTON", "rt_price", MODEL)
    assert book.latest(T - pd.Timedelta(microseconds=1)) is None
    assert book.latest(T) == 0
    assert book.latest(T + pd.Timedelta(minutes=59)) == 0
    assert book.latest(T + pd.Timedelta(hours=1)) == 1
    assert book.latest(T + pd.Timedelta(days=30)) == 1
    assert book.issued[0] == T.value  # int64 ns, even though pandas 3 infers microseconds
    assert (np.diff(book.point_times(0)) == 15 * 60 * 10**9).all()


def test_latest_forecast_matches_the_api_shape(cache):
    f = latest_forecast("LZ_HOUSTON", "2025-05-16T21:30:00Z", model=MODEL, source="parquet", cache_dir=cache)
    assert set(f) == {"location", "series", "model", "issued_utc", "step_minutes", "points"}
    assert (f["location"], f["series"], f["model"], f["step_minutes"]) == ("LZ_HOUSTON", "rt_price", MODEL, 15)
    assert f["issued_utc"] == "2025-05-16T21:00:00Z"
    assert len(f["points"]) == 96
    first, last = f["points"][0], f["points"][-1]
    assert first == {"ts_utc": "2025-05-16T21:00:00Z", "p10": 0.0, "p50": 10.0, "p90": 20.0}
    assert last["ts_utc"] == "2025-05-17T20:45:00Z"
    demand = latest_forecast("ERCOT", "2025-05-17T00:00:00Z", "demand", model=MODEL, source="parquet",
                             cache_dir=cache)
    assert demand["step_minutes"] == 60 and len(demand["points"]) == 48
    assert demand["points"][1]["ts_utc"] == "2025-05-16T21:00:00Z"


def test_latest_forecast_none_before_the_first_issue_and_needs_aware_time(cache):
    assert latest_forecast("LZ_HOUSTON", "2025-05-16T19:59:59Z", source="parquet", cache_dir=cache) is None
    assert latest_forecast("LZ_WEST", "2025-06-01T00:00:00Z", source="parquet", cache_dir=cache) is None
    with pytest.raises(ValueError):
        latest_forecast("LZ_HOUSTON", "2025-05-16T21:00:00", source="parquet", cache_dir=cache)


def test_empty_book_is_harmless(tmp_path):
    book = ForecastBook.load("LZ_HOUSTON", model=MODEL, source="parquet", cache_dir=tmp_path)
    assert len(book) == 0 and book.latest(T) is None


def test_default_source_follows_the_environment(monkeypatch):
    monkeypatch.delenv("FORECAST_SOURCE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    assert store.default_source() == "parquet"
    monkeypatch.setenv("DB_HOST", "/cloudsql/x")
    assert store.default_source() == "db"
    monkeypatch.setenv("FORECAST_SOURCE", "parquet")
    assert store.default_source() == "parquet"


# --- Postgres (skips cleanly when no database is reachable) --------------------------------


def _database():
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=2):
            return url
    except Exception:
        return None


@pytest.mark.postgres
def test_postgres_roundtrip():
    if _database() is None:
        pytest.skip("no reachable Postgres (set DATABASE_URL, e.g. scripts/dev/services.sh up)")
    from src import db

    db.migrate()
    model = f"test-{uuid.uuid4().hex[:8]}"  # isolates this test's rows
    sink = store.DbSink()
    try:
        rows = [synthetic_row(T + pd.Timedelta(hours=h), model=model, level=float(h)) for h in range(3)]
        assert sink.write(rows) == 3
        assert sink.write(rows[:1]) == 1  # upsert, no duplicate
        assert sink.existing(model, "rt_price", "LZ_HOUSTON", T, T + pd.Timedelta(days=1)) == {r.issued_at for r in rows}
        f = latest_forecast("LZ_HOUSTON", T + pd.Timedelta(minutes=90), model=model, source="db")
        assert f["issued_utc"] == "2025-05-16T21:00:00Z" and f["points"][0]["p50"] == 1.0
        assert latest_forecast("LZ_HOUSTON", T - pd.Timedelta(seconds=1), model=model, source="db") is None
        book = ForecastBook.load("LZ_HOUSTON", model=model, source="db")
        assert len(book) == 3 and book.horizon == 96 and book.step_minutes == 15
    finally:
        with db.connect(autocommit=True) as conn:
            conn.execute("DELETE FROM forecasts WHERE model = %s", (model,))
        sink.close()
