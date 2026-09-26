"""PostgresReplayFeed: the PriceFeed seam over the prices table (spec section 11, feeds/replay.py).

It reads the native rows for one location in bulk, one query per month, and
yields 5-min PriceTicks. Each interval's price is held for its own ticks only
(RT 15-min -> 3 ticks, DA hourly -> 12), the same as
src.feeds.memory.forward_fill. A missing interval stays missing and nothing
is filled across a gap. RT rows carry the demand_mw and fuel_mix context the
ingest stored; DA rows carry None. It passes tests/test_contracts_pricefeed.py.

    feed = PostgresReplayFeed()               # market="RT", DATABASE_URL / DB_HOST via src.db
    for tick in feed.ticks(start, end, "LZ_HOUSTON"):
        ...
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Iterable, Iterator

from psycopg import errors, sql

from src.core.models import PriceTick

UTC = timezone.utc
INTERVAL_MINUTES = {"RT": 15, "DA": 60}
_ONE_US = timedelta(microseconds=1)


@lru_cache(maxsize=1)
def database_available(timeout_s: int = 3) -> bool:
    """True when DATABASE_URL / DB_HOST is set and Postgres answers (checked once per process)."""
    if not (os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST")):
        return False
    from src.db import connect

    try:
        with connect(connect_timeout=timeout_s):
            return True
    except Exception:
        return False


class PostgresReplayFeed:
    """PriceFeed over `prices` rows of one market, forward-filled to 5-min ticks."""

    def __init__(self, market: str = "RT", *, conn=None, table: str = "prices",
                 chunk: timedelta = timedelta(days=31), tick_minutes: int = 5) -> None:
        market = market.upper()
        if market not in INTERVAL_MINUTES:
            raise ValueError(f"market must be RT or DA, got {market!r}")
        interval_minutes = INTERVAL_MINUTES[market]
        if tick_minutes <= 0 or interval_minutes % tick_minutes:
            raise ValueError("the interval must be a positive multiple of tick_minutes")
        if chunk <= timedelta(0):
            raise ValueError("chunk must be positive")
        self.market = market
        self._interval = timedelta(minutes=interval_minutes)
        self._interval_s = interval_minutes * 60
        self._tick = timedelta(minutes=tick_minutes)
        self._per_interval = interval_minutes // tick_minutes
        self._chunk = chunk
        self._conn = conn
        self._owns_conn = conn is None  # close() closes only a connection this feed opened
        self._reconnect = conn is None  # a dropped connection of our own is reopened once
        self._query = sql.SQL(
            "SELECT ts, price_usd_mwh, demand_mw, fuel_mix FROM {} "
            "WHERE location = %s AND market = %s AND ts >= %s AND ts < %s ORDER BY ts"
        ).format(sql.Identifier(*table.split(".")))

    # --- PriceFeed ---------------------------------------------------------------------

    def ticks(self, start: datetime, end: datetime, location: str) -> Iterator[PriceTick]:
        _require_aware(start, "start")
        _require_aware(end, "end")
        start, end = start.astimezone(UTC), end.astimezone(UTC)
        if start >= end:
            return iter(())
        return self._generate(start, end, location)

    def _generate(self, start: datetime, end: datetime, location: str) -> Iterator[PriceTick]:
        tick, per_interval = self._tick, self._per_interval
        lo = start - self._interval + _ONE_US  # first interval start whose ticks can reach `start`
        while lo < end:
            hi = min(lo + self._chunk, end)
            for ts, price, demand, mix in self._fetch(location, lo, hi):
                ts = ts.astimezone(UTC)
                if ts.microsecond or int(ts.timestamp()) % self._interval_s:
                    raise ValueError(f"{ts.isoformat()} is not on the {self._interval_s // 60}-minute grid")
                if price is None or not math.isfinite(price):
                    raise ValueError(f"non-finite price at {ts.isoformat()} {location}")
                price = float(price)
                demand = None if demand is None else float(demand)
                for k in range(per_interval):
                    t = ts + k * tick
                    if start <= t < end:
                        yield PriceTick(t, location, price, demand, mix)
            lo = hi

    # --- connection --------------------------------------------------------------------

    def _fetch(self, location: str, lo: datetime, hi: datetime) -> list[tuple]:
        params = (location, self.market, lo, hi)
        try:
            with self._connection().cursor() as cur:
                cur.execute(self._query, params)
                return cur.fetchall()
        except errors.OperationalError:
            if not self._reconnect:
                raise
            self.close()  # dropped connection: reconnect once
            with self._connection().cursor() as cur:
                cur.execute(self._query, params)
                return cur.fetchall()

    def _connection(self):
        if self._conn is None or self._conn.closed:
            from src.db import connect

            self._conn = connect(autocommit=True)
        return self._conn

    def close(self) -> None:
        """Close the connection if this feed opened it (a borrowed one is left alone)."""
        if self._owns_conn and self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> PostgresReplayFeed:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # --- private rows ------------------------------------------------------------------

    @classmethod
    def from_rows(cls, rows: Iterable[PriceTick], market: str = "RT") -> PostgresReplayFeed:
        """A feed over `rows` only, for tests and demos.

        `rows` are native interval rows, exactly as the prices table stores
        them (ts = interval start). They go into a TEMP table on a private
        connection, so nothing else sees them. They disappear when the feed is
        closed or garbage-collected. Duplicate (ts, location) rows raise
        ValueError.
        """
        from src.db import connect

        market = market.upper()
        conn = connect(autocommit=True)
        try:
            conn.execute("CREATE TEMP TABLE replay_rows (ts timestamptz NOT NULL, location text NOT NULL, "
                         "market text NOT NULL, price_usd_mwh double precision NOT NULL, "
                         "demand_mw double precision, fuel_mix jsonb, PRIMARY KEY (ts, location, market))")
            with conn.cursor() as cur, cur.copy("COPY replay_rows (ts, location, market, price_usd_mwh, "
                                                "demand_mw, fuel_mix) FROM STDIN") as copy:
                for r in rows:
                    _require_aware(r.ts, "row ts")
                    mix = None if r.fuel_mix is None else json.dumps(r.fuel_mix)
                    copy.write_row((r.ts, r.location, market, r.price_usd_mwh, r.demand_mw, mix))
        except errors.UniqueViolation as exc:
            conn.close()
            raise ValueError(f"duplicate (ts, location) rows: {exc}") from exc
        except BaseException:
            conn.close()
            raise
        feed = cls(market, conn=conn, table="pg_temp.replay_rows")
        feed._owns_conn = True
        return feed


def _require_aware(ts: datetime, what: str) -> None:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware, got {ts!r}")
