"""PRICE_SOURCE=postgres (src.feeds.replay.PostgresReplayFeed(market='RT'), the prices table) drives the
fleet exactly like the default snapshot source: same ticks, same decisions, same SoC, same ledger.
Skips unless Postgres is reachable and holds the real RT prices for the replayed window (`make ingest`)."""
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from src.sim import coordinator
from src.sim.redis_layout import reachable, redis_url

pytestmark = [pytest.mark.redis, pytest.mark.postgres]
UTC = timezone.utc
START = "2025-05-16T21:00:00Z"


def _prices_loaded() -> bool:
    from src.feeds.replay import database_available

    if not database_available():
        return False
    from src.db import connect

    start = datetime(2025, 5, 9, 21, tzinfo=UTC)
    with connect() as conn:
        n = conn.execute("SELECT count(*) FROM prices WHERE market = 'RT' AND location IN ('LZ_HOUSTON', 'LZ_NORTH') "
                         "AND ts >= %s AND ts < %s", (start, start + timedelta(days=8))).fetchone()[0]
    return n >= 2 * 8 * 96


def test_postgres_prices_replay_like_the_snapshot(monkeypatch):
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    if not _prices_loaded():
        pytest.skip("Postgres not reachable or its prices table lacks the real 2025 RT rows (make ingest)")
    runs = {}
    for source in ("snapshot", "postgres"):
        monkeypatch.setenv("PRICE_SOURCE", source)
        runs[source] = coordinator.run_fleet_replay("bands", start_utc=START, ticks=72, soc_init=0.9, fleet_size=400,
                                                    workers=2, url=url, prefix=f"test:src:{uuid.uuid4().hex[:8]}:")
    from src.feeds.replay import PostgresReplayFeed

    assert any(isinstance(f, PostgresReplayFeed) and f.market == "RT" for f in coordinator._FEEDS.values())
    a, b = runs["snapshot"], runs["postgres"]
    assert a["summary"] == b["summary"] and np.array_equal(a["soc"], b["soc"])
    for loc in a["locations"]:
        assert np.array_equal(a["locations"][loc]["target_kw"], b["locations"][loc]["target_kw"])
        assert np.array_equal(a["locations"][loc]["delivered_kwh"], b["locations"][loc]["delivered_kwh"])
