"""Both series backends agree wherever both have data (real ERCOT data, read-only).

Compares src.data.series_pg (Postgres, filled by `python -m src.ingest.ercot`)
with the snapshot backend over their whole overlap: RT and DA 2024-2025 for
the 7 snapshot locations, native load 2025 and fuel mix 2025. Skips unless
Postgres is reachable AND the ingest has loaded 2024-2025.

Known, documented differences (docs/notes/data.md):
- system_load 2025-11-02 06:00 and 07:00 UTC: the gridstatus-built snapshot
  had the fall-back hour's two passes swapped. The integrator corrected the
  snapshot to ERCOT's order on 2026-09-25, so both backends now agree there.
- 4 system_load values differ in the last bit: the workbook stores 17
  significant digits, and the snapshot holds them rounded.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.data import series, series_pg
from src.feeds.replay import database_available
from src.ingest.report import describe_load_diff
from src.ingest.store import year_window

pytestmark = pytest.mark.postgres
UTC = timezone.utc
SNAP = series.SNAPSHOT_DIR


@pytest.fixture(scope="module", autouse=True)
def ingested():
    if not database_available():
        pytest.skip("Postgres not reachable: set DATABASE_URL (scripts/dev/services.sh up)")
    from src.db import connect

    with connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
        if not {"prices", "native_load", "generation_by_fuel"} <= tables:
            pytest.skip("ingest tables missing: run make ingest")
        for year in (2024, 2025):
            a, b = year_window(year)
            n = conn.execute("SELECT count(*) FROM prices WHERE location = 'LZ_HOUSTON' AND market = 'RT' "
                             "AND ts >= %s AND ts < %s", (a, b)).fetchone()[0]
            if n < 35040:
                pytest.skip(f"ERCOT {year} not ingested: run make ingest")


@pytest.fixture(autouse=True)
def snapshot_env(monkeypatch):
    monkeypatch.setenv("SERIES_BACKEND", "snapshot")  # `series.*` = snapshot, `series_pg.*` = Postgres


def snapshot_locations(market: str, year: int) -> list[str]:
    return sorted(pd.read_parquet(SNAP / f"ercot_{market}_{year}_hubs.parquet", columns=["location"])["location"].unique())


@pytest.mark.parametrize("year", [2024, 2025])
@pytest.mark.parametrize("market", ["rt", "da"])
def test_prices_identical_for_every_snapshot_location(market, year):
    a, b = year_window(year)
    snap_fn, pg_fn = (series.rt_prices, series_pg.rt_prices) if market == "rt" else (series.da_prices, series_pg.da_prices)
    locations = snapshot_locations(market, year)
    assert len(locations) == 7
    for loc in locations:
        pd.testing.assert_series_equal(pg_fn(loc, a, b), snap_fn(loc, a, b), check_exact=True)


@pytest.mark.parametrize("start, end", [
    ("2024-03-10T06:00Z", "2024-03-11T05:00Z"),  # spring-forward local day (23 h)
    ("2024-11-03T05:00Z", "2024-11-04T06:00Z"),  # fall-back local day (25 h)
    ("2025-11-02T05:17Z", "2025-11-02T09:02Z"),  # off-grid bounds through the repeated hour
    ("2024-12-31T20:00Z", "2025-01-01T12:00Z"),  # across the 2024/2025 file boundary
    ("2025-05-16T21:00Z", "2025-05-17T02:00Z"),  # the demo's spike evening
])
def test_windows_identical(start, end):
    a, b = pd.Timestamp(start), pd.Timestamp(end)
    for loc in ("LZ_HOUSTON", "LZ_NORTH", "HB_NORTH"):
        pd.testing.assert_series_equal(series_pg.rt_prices(loc, a, b), series.rt_prices(loc, a, b), check_exact=True)
        pd.testing.assert_series_equal(series_pg.da_prices(loc, a, b), series.da_prices(loc, a, b), check_exact=True)


def test_fuel_mix_2025_identical():
    a, b = year_window(2025)
    pd.testing.assert_frame_equal(series_pg.fuel_mix(a, b), series.fuel_mix(a, b), check_exact=True)


def test_system_load_2025_identical_but_for_last_bit_rounding():
    a, b = year_window(2025)
    pg, snap = series_pg.system_load(a, b), series.system_load(a, b)
    assert pg.name == snap.name and pg.index.name == snap.index.name and pg.dtype == snap.dtype
    pd.testing.assert_index_equal(pg.index, snap.index)
    description, ok = describe_load_diff(snap, pg)
    assert ok, description
    h1, h2 = pd.Timestamp("2025-11-02T06:00Z"), pd.Timestamp("2025-11-02T07:00Z")  # 01:00 CDT, 01:00 CST
    assert pg[h1] == snap[h1] and pg[h2] == snap[h2]  # fall-back hour in ERCOT's order in both
    assert np.allclose(pg, snap, rtol=4 * np.finfo(float).eps, atol=0)
    assert (pg != snap).sum() == 4
    assert pg[h1] > pg[h2]  # ERCOT's order: overnight load falls from the first pass to the second


def test_the_public_api_dispatches_to_postgres(monkeypatch):
    monkeypatch.delenv("SERIES_BACKEND")
    assert series.backend() == "postgres"
    a, b = pd.Timestamp("2025-05-16T21:00Z"), pd.Timestamp("2025-05-17T02:00Z")
    got = series.rt_prices("LZ_HOUSTON", a, b)
    assert got.max() == 2250.15 and pd.Timestamp("2025-05-17T00:45Z") == got.idxmax()  # 19:45 CDT peak
    monkeypatch.setenv("SERIES_BACKEND", "snapshot")
    pd.testing.assert_series_equal(got, series.rt_prices("LZ_HOUSTON", a, b), check_exact=True)


def test_postgres_covers_the_hours_the_snapshot_backend_misses_at_new_year():
    """2026-01-01 00:00-06:00 UTC is still ERCOT 2025 (in the 2025 file). Both backends return it
    (the snapshot backend used to miss it; fixed by reading the previous year's file too)."""
    a, b = datetime(2026, 1, 1, 0, tzinfo=UTC), datetime(2026, 1, 1, 6, tzinfo=UTC)
    assert len(series_pg.rt_prices("LZ_HOUSTON", a, b)) == 24
    pd.testing.assert_series_equal(series_pg.rt_prices("LZ_HOUSTON", a, b), series.rt_prices("LZ_HOUSTON", a, b), check_exact=True)
