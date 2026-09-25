"""PostgresReplayFeed (src/feeds/replay.py) beyond the shared PriceFeed contract suite.

Synthetic-row tests use private TEMP tables (PostgresReplayFeed.from_rows), and
their prices are SYNTHETIC. The real-data tests read the ingested 2025 prices
and skip unless `make ingest` has run. Everything skips when Postgres is not
reachable.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.core.models import PriceTick
from src.feeds.memory import InMemoryPriceFeed
from src.feeds.replay import PostgresReplayFeed, database_available

pytestmark = pytest.mark.postgres
UTC = timezone.utc
T0 = datetime(2025, 3, 9, 6, tzinfo=UTC)  # 2025-03-09 00:00 CST, the spring-forward day
FIVE = timedelta(minutes=5)


@pytest.fixture(autouse=True)
def need_db():
    if not database_available():
        pytest.skip("Postgres not reachable: set DATABASE_URL (scripts/dev/services.sh up)")


def rows(n: int, step_min: int = 15, location: str = "LZ_A", skip=()) -> list[PriceTick]:
    """SYNTHETIC native interval rows."""
    return [PriceTick(T0 + timedelta(minutes=step_min * i), location, 10.0 + i, 40000.0 + i, {"gas": 0.5, "wind": 0.5})
            for i in range(n) if i not in skip]


def as_tuples(ticks):
    return [(t.ts, t.location, t.price_usd_mwh, t.demand_mw, t.fuel_mix) for t in ticks]


def test_da_rows_fill_twelve_ticks_per_hour_and_never_across_a_gap():
    with PostgresReplayFeed.from_rows(rows(6, step_min=60, skip={2}), market="DA") as feed:
        got = list(feed.ticks(T0, T0 + timedelta(hours=6), "LZ_A"))
    assert len(got) == 5 * 12
    assert [t.ts for t in got[:12]] == [T0 + k * FIVE for k in range(12)] and {t.price_usd_mwh for t in got[:12]} == {10.0}
    assert all(not (T0 + timedelta(hours=2) <= t.ts < T0 + timedelta(hours=3)) for t in got)


@pytest.mark.parametrize("chunk_minutes", [5, 20, 45, 60 * 24 * 31])
def test_chunk_boundaries_change_nothing(chunk_minutes):
    src = rows(40, skip={7, 8, 30})
    expected = as_tuples(InMemoryPriceFeed.from_intervals(src).ticks(T0 + FIVE * 2, T0 + timedelta(hours=9), "LZ_A"))
    with PostgresReplayFeed.from_rows(src) as feed:
        feed._chunk = timedelta(minutes=chunk_minutes)
        got = as_tuples(feed.ticks(T0 + FIVE * 2, T0 + timedelta(hours=9), "LZ_A"))
    assert got == expected
    assert len(got) == 1 + 3 * 32  # intervals 0-35 minus 3 gaps; interval 0 starts 10 min before `start`


def test_ticks_are_utc_python_datetimes_and_context_passes_through():
    with PostgresReplayFeed.from_rows(rows(2)) as feed:
        got = list(feed.ticks(T0, T0 + timedelta(hours=1), "LZ_A"))
    assert all(t.ts.tzinfo is UTC for t in got) and got[0].demand_mw == 40000.0
    assert got[0].fuel_mix == {"gas": 0.5, "wind": 0.5} and got[0].fuel_mix is got[2].fuel_mix  # one dict per interval


def test_off_grid_row_raises():
    bad = rows(2) + [PriceTick(T0 + timedelta(minutes=37), "LZ_A", 1.0, None, None)]
    with PostgresReplayFeed.from_rows(bad) as feed, pytest.raises(ValueError, match="grid"):
        list(feed.ticks(T0, T0 + timedelta(hours=1), "LZ_A"))


def test_non_finite_price_raises():
    bad = [PriceTick(T0, "LZ_A", math.nan, None, None)]
    with PostgresReplayFeed.from_rows(bad) as feed, pytest.raises(ValueError, match="non-finite"):
        list(feed.ticks(T0, T0 + timedelta(hours=1), "LZ_A"))


def test_duplicate_rows_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        PostgresReplayFeed.from_rows(rows(2) + rows(1))


def test_a_borrowed_connection_is_left_open():
    from src.db import connect

    with connect(autocommit=True) as conn:
        feed = PostgresReplayFeed(conn=conn)
        feed.close()
        assert not conn.closed
    with pytest.raises(ValueError):
        PostgresReplayFeed(market="ID")


# --- real ingested data -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def ingested_2025():
    if not database_available():  # module fixtures run before the function-scoped need_db
        pytest.skip("Postgres not reachable: set DATABASE_URL (scripts/dev/services.sh up)")
    from src.db import connect

    with connect() as conn:
        n = conn.execute("SELECT count(*) FROM prices WHERE market = 'RT' AND location IN ('LZ_HOUSTON', 'LZ_NORTH') "
                         "AND ts >= '2025-01-01 06:00+00' AND ts < '2026-01-01 06:00+00'").fetchone()[0]
    if n < 2 * 35040:
        pytest.skip("ERCOT 2025 not ingested: run make ingest")


def test_full_year_for_two_locations_streams_in_seconds_and_matches_the_snapshot(ingested_2025):
    a, b = datetime(2025, 1, 1, 6, tzinfo=UTC), datetime(2026, 1, 1, 6, tzinfo=UTC)
    snap = pd.read_parquet("data/snapshot/ercot_rt_2025_hubs.parquet")
    feed = PostgresReplayFeed()
    t0 = time.perf_counter()
    streamed = {loc: list(feed.ticks(a, b, loc)) for loc in ("LZ_HOUSTON", "LZ_NORTH")}
    elapsed = time.perf_counter() - t0
    feed.close()
    assert elapsed < 5.0, f"{elapsed:.1f}s"  # measured about 0.6 s on the dev laptop
    for loc, ticks in streamed.items():
        assert len(ticks) == 3 * 35040
        s = snap[snap.location == loc].sort_values("ts")
        starts = s.ts.dt.tz_convert(None).to_numpy().astype("datetime64[ns]")
        want_ts = np.repeat(starts, 3) + np.tile(np.array([0, 5, 10], dtype="timedelta64[m]"), len(s))
        got_ts = pd.DatetimeIndex([t.ts for t in ticks]).tz_convert(None).as_unit("ns").to_numpy()
        assert np.array_equal(got_ts, want_ts)
        assert np.array_equal(np.array([t.price_usd_mwh for t in ticks]), np.repeat(s.price_usd_mwh.to_numpy(), 3))
        assert all(t.demand_mw is not None and t.fuel_mix is not None for t in ticks)
        assert all(abs(sum(t.fuel_mix.values()) - 1) < 1e-12 for t in ticks[::997])
