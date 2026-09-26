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
