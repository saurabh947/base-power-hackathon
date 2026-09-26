"""The AI's prediction in Postgres (migrations/030_forecast_predictions.sql) and where 2026's real prices end
according to the prices table. Needs Postgres (skips otherwise).

The storage test runs in a throwaway schema (001 + 030 applied there), with SYNTHETIC paths. The real_until test
reads the real prices table after `python -m src.ingest.ercot --years 2025 2026`, read-only.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager

import numpy as np
import pandas as pd
import pytest

from src.data import years
from src.db import MIGRATIONS_DIR, connect
from src.feeds.replay import database_available
from src.forecast import predictions

pytestmark = pytest.mark.postgres
CUTOFF = pd.Timestamp("2026-09-20T05:00Z")


@pytest.fixture(autouse=True)
def need_postgres():
    if not database_available():
        pytest.skip("Postgres not reachable: set DATABASE_URL (scripts/dev/services.sh up)")


@contextmanager
def scratch():
    conn = connect(autocommit=True)
    name = f"scratch_{uuid.uuid4().hex[:12]}"
    try:
        conn.execute(f'CREATE SCHEMA "{name}"')
        conn.execute(f'SET search_path TO "{name}"')
        for path in (MIGRATIONS_DIR / "001_init.sql", MIGRATIONS_DIR / "030_forecast_predictions.sql"):
            conn.execute(path.read_text())
        yield conn
    finally:
        try:
            conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        finally:
            conn.close()


def path(loc="LZ_HOUSTON", cutoff=CUTOFF, n=500, base=40.0):
    """SYNTHETIC prediction path."""
    ts = pd.Timestamp(cutoff).value + np.arange(n, dtype=np.int64) * 15 * 60 * 10**9
    p50 = np.round(base + np.arange(n) * 0.01, 2)
    return predictions.PredictionPath(loc, "timesfm-3.0", pd.Timestamp(cutoff), pd.Timestamp("2026-09-26T10:00Z"), ts,
                                      p50 - 4, p50, p50 + 4, (np.arange(n) // 672).astype(np.int16))


def test_db_paths_round_trip_and_the_same_cutoff_replaces_its_rows():
    with scratch() as conn:
        a = path()
        predictions.write([a, path("LZ_NORTH")], out="db", conn=conn)
        got = predictions.load("LZ_HOUSTON", model="timesfm-3.0", conn=conn)
        for field in ("ts", "p10", "p50", "p90", "chunk"):
            assert np.array_equal(getattr(got, field), getattr(a, field)), field
        assert got.cutoff == CUTOFF and got.issued_at == a.issued_at
        # idempotent: the same cutoff again (a re-run) replaces that path, and only it
        predictions.write([path(n=300, base=55.0)], out="db", conn=conn)
        counts = dict(conn.execute("SELECT location, count(*) FROM price_predictions GROUP BY location").fetchall())
        assert counts == {"LZ_HOUSTON": 300, "LZ_NORTH": 500}
        assert predictions.load("LZ_HOUSTON", model="timesfm-3.0", conn=conn).p50[0] == 55.0
        # a later cutoff (a newer ERCOT publication) adds a path; readers take the latest at or before real_until
        later = CUTOFF + pd.Timedelta(days=7)
        predictions.write([path(cutoff=later, base=70.0)], out="db", conn=conn)
        assert predictions.load("LZ_HOUSTON", max_cutoff=later, model="timesfm-3.0", conn=conn).p50[0] == 70.0
        assert predictions.load("LZ_HOUSTON", max_cutoff=later - pd.Timedelta(days=1), model="timesfm-3.0",
                                conn=conn).p50[0] == 55.0
        assert predictions.load("LZ_AEN", model="timesfm-3.0", conn=conn) is None
        # every stored point is at or after its cutoff: nothing predicted what was already real
        assert conn.execute("SELECT count(*) FROM price_predictions WHERE ts < cutoff").fetchone()[0] == 0


@pytest.mark.parametrize("case", ["one_city_stops_early", "hole_at_the_last_interval", "long_holes_at_the_end"])
def test_real_until_is_the_last_interval_every_city_has(case):
    """SYNTHETIC prices in a scratch schema: the fast edge query agrees with grouping the whole year."""
    from src.data.years import _COMMON_SQL, _common_range_db, year_window

    locs = ("LZ_HOUSTON", "LZ_NORTH", "LZ_CPS", "LZ_AEN")
    start = pd.Timestamp("2026-01-01T06:00Z")
    n = 96 * 10
    ts = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    rows = {loc: set(range(n)) for loc in locs}
    if case == "one_city_stops_early":
        rows["LZ_CPS"] -= set(range(n - 3, n))
        want = ts[n - 4]
    elif case == "hole_at_the_last_interval":
        rows["LZ_AEN"] -= {n - 1}
        rows["LZ_NORTH"] -= {n - 1}
        want = ts[n - 2]
    else:  # every city misses a different stretch of the last 3 days: the fast windows find nothing in common
        for k, loc in enumerate(locs):
            rows[loc] -= set(range(n - 96 * 3 + k, n, 4))
        want = None
    with scratch() as conn:
        with conn.cursor() as cur, cur.copy("COPY prices (ts, location, market, price_usd_mwh) FROM STDIN") as copy:
            for loc in locs:
                for i in sorted(rows[loc]):
                    copy.write_row((ts[i].to_pydatetime(), loc, "RT", 20.0))
        first, last = _common_range_db(2026, locs, conn)
        a, b = year_window(2026)
        full = conn.execute(_COMMON_SQL, (list(locs), a, b, 4)).fetchone()
    assert (first, last) == (full[0], full[1]) and first == start
    if want is not None:
        assert last == want


def test_real_until_from_postgres_matches_the_snapshot_when_they_hold_the_same_publication():
    snap = years.info(2026, source="snapshot", refresh=True)
    pg = years.info(2026, source="postgres", refresh=True)
    if not pg.available:
        pytest.skip("2026 not ingested: run python -m src.ingest.ercot --years 2026")
    assert pg.first_utc == pd.Timestamp("2026-01-01T06:00Z")
    if pg.real_until_utc != snap.real_until_utc:
        pytest.skip(f"Postgres holds another 2026 publication (real until {pg.real_until_utc})")
    assert pg.real_until_utc == CUTOFF
    y25 = years.info(2025, source="postgres", refresh=True)
    assert y25.available and y25.real_until_utc is None
