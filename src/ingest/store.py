"""Idempotent Postgres loads for the ERCOT ingest.

Each load replaces one (dataset, year) slice in a single transaction:
1. COPY the parsed rows into a temp staging table (floats are written as
   their shortest exact repr, so values round-trip bit for bit).
2. Upsert. A row is written only when its values changed, so a re-run
   writes nothing.
3. Delete rows of the same slice that the new file no longer has, e.g. rows
   placed at a wrong DST instant by an older parser.
A crash anywhere leaves the previous data untouched.

Tables and units are documented in migrations/010_ingest_tables.sql.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

TZ = "America/Chicago"
_COPY_CHUNK = 1 << 23

# Context carried on RT price rows for the replay feed: ERCOT native load for
# the hour containing the interval, and the interval's fuel shares as jsonb.
_RT_CONTEXT_JOINS = """
    LEFT JOIN native_load d ON d.zone = 'ERCOT' AND d.ts = date_trunc('hour', {alias}.ts, 'UTC')
    LEFT JOIN (
        SELECT ts, jsonb_object_agg(lower(fuel), share) AS fuel_mix
        FROM generation_by_fuel
        WHERE share IS NOT NULL AND ts >= %(a)s AND ts < %(b)s
        GROUP BY ts
    ) m ON m.ts = {alias}.ts
"""


@dataclass(frozen=True)
class LoadCounts:
    """What one slice load did. unchanged = rows already present with identical values."""

    staged: int
    inserted: int
    updated: int
    deleted: int

    @property
    def unchanged(self) -> int:
        return self.staged - self.inserted - self.updated

    def __str__(self) -> str:
        return (f"{self.staged} rows: {self.inserted} inserted, {self.updated} updated, "
                f"{self.unchanged} unchanged, {self.deleted} stale deleted")


def year_window(year: int) -> tuple[datetime, datetime]:
    """UTC [start, end) of an ERCOT calendar year: America/Chicago midnight Jan 1 to midnight Jan 1."""
    a = pd.Timestamp(year=year, month=1, day=1, tz=TZ).tz_convert("UTC")
    b = pd.Timestamp(year=year + 1, month=1, day=1, tz=TZ).tz_convert("UTC")
    return a.to_pydatetime(), b.to_pydatetime()


def _require_inside(ts: pd.Series, year: int, what: str) -> None:
    a, b = year_window(year)
    outside = (ts < pd.Timestamp(a)) | (ts >= pd.Timestamp(b))
    if outside.any():
        raise ValueError(f"{what}: {int(outside.sum())} rows fall outside ERCOT year {year}, e.g. {ts[outside].iloc[0]}")


def _copy(cur, table: str, frame: pd.DataFrame, columns: list[str]) -> None:
    buf = io.StringIO()
    frame.to_csv(buf, index=False, header=False, columns=columns)  # NaN/None -> empty -> NULL
    data = buf.getvalue()
    with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN (FORMAT csv)") as cp:
        for i in range(0, len(data), _COPY_CHUNK):
            cp.write(data[i:i + _COPY_CHUNK])


def _upsert_counts(cur, sql: str, params: dict) -> tuple[int, int]:
    cur.execute(f"WITH up AS ({sql} RETURNING (xmax = 0) AS inserted) "
                "SELECT count(*) FILTER (WHERE inserted), count(*) FILTER (WHERE NOT inserted) FROM up", params)
    inserted, updated = cur.fetchone()
    return int(inserted), int(updated)


# --- prices ----------------------------------------------------------------------


def upsert_prices(conn, frame: pd.DataFrame, market: str, year: int) -> LoadCounts:
    """Replace the (market, year) slice of `prices` with `frame` (columns ts, location, price_usd_mwh).

    RT rows get demand_mw and fuel_mix from native_load and generation_by_fuel
    as they stand in the database, so load those first. DA rows keep them NULL.
    """
    market = market.upper()
    _require_inside(frame["ts"], year, f"{market} prices")
    a, b = year_window(year)
    params = {"a": a, "b": b, "market": market}
    if market == "RT":
        select = ("SELECT s.ts, s.location, 'RT', s.price_usd_mwh, d.load_mw, m.fuel_mix FROM _stage_prices s"
                  + _RT_CONTEXT_JOINS.format(alias="s"))
    else:
        select = "SELECT s.ts, s.location, %(market)s, s.price_usd_mwh, NULL::float8, NULL::jsonb FROM _stage_prices s"
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _stage_prices (ts timestamptz NOT NULL, location text NOT NULL, "
                    "price_usd_mwh double precision NOT NULL, PRIMARY KEY (location, ts)) ON COMMIT DROP")
        _copy(cur, "_stage_prices", frame, ["ts", "location", "price_usd_mwh"])
        cur.execute("ANALYZE _stage_prices")
        inserted, updated = _upsert_counts(cur, f"""
            INSERT INTO prices AS p (ts, location, market, price_usd_mwh, demand_mw, fuel_mix)
            {select}
            ON CONFLICT (ts, location, market) DO UPDATE
               SET price_usd_mwh = EXCLUDED.price_usd_mwh, demand_mw = EXCLUDED.demand_mw, fuel_mix = EXCLUDED.fuel_mix
             WHERE (p.price_usd_mwh, p.demand_mw, p.fuel_mix)
                   IS DISTINCT FROM (EXCLUDED.price_usd_mwh, EXCLUDED.demand_mw, EXCLUDED.fuel_mix)""", params)
        cur.execute("""
            DELETE FROM prices p
             WHERE p.market = %(market)s AND p.ts >= %(a)s AND p.ts < %(b)s
               AND p.location IN (SELECT DISTINCT location FROM _stage_prices)
               AND NOT EXISTS (SELECT 1 FROM _stage_prices s WHERE s.location = p.location AND s.ts = p.ts)""", params)
        deleted = cur.rowcount
    return LoadCounts(len(frame), inserted, updated, deleted)


def enrich_rt(conn, year: int) -> int:
    """Refresh demand_mw / fuel_mix on every RT row of `year` from the load and fuel tables.

    Writes only rows whose context changed (none after a normal ingest, which
    loads load and fuel before RT). Returns the number of rows updated.
    """
    a, b = year_window(year)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"""
            WITH want AS (
                SELECT p.ts, p.location, d.load_mw AS demand_mw, m.fuel_mix
                  FROM prices p
                  {_RT_CONTEXT_JOINS.format(alias="p")}
                 WHERE p.market = 'RT' AND p.ts >= %(a)s AND p.ts < %(b)s
            )
            UPDATE prices p SET demand_mw = w.demand_mw, fuel_mix = w.fuel_mix
              FROM want w
             WHERE p.market = 'RT' AND p.ts = w.ts AND p.location = w.location
               AND (p.demand_mw, p.fuel_mix) IS DISTINCT FROM (w.demand_mw, w.fuel_mix)""", {"a": a, "b": b})
        return cur.rowcount


# --- native load -------------------------------------------------------------------


def upsert_native_load(conn, wide: pd.DataFrame, year: int) -> LoadCounts:
    """Replace the `year` slice of native_load with a wide hourly frame (index ts, one column per zone)."""
    long = wide.rename_axis(index="ts", columns="zone").stack().rename("load_mw").reset_index()
    _require_inside(long["ts"], year, "native load")
    a, b = year_window(year)
    params = {"a": a, "b": b}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _stage_load (ts timestamptz NOT NULL, zone text NOT NULL, "
                    "load_mw double precision NOT NULL, PRIMARY KEY (zone, ts)) ON COMMIT DROP")
        _copy(cur, "_stage_load", long, ["ts", "zone", "load_mw"])
        cur.execute("ANALYZE _stage_load")
        inserted, updated = _upsert_counts(cur, """
            INSERT INTO native_load AS n (ts, zone, load_mw)
            SELECT ts, zone, load_mw FROM _stage_load
            ON CONFLICT (zone, ts) DO UPDATE SET load_mw = EXCLUDED.load_mw
             WHERE n.load_mw IS DISTINCT FROM EXCLUDED.load_mw""", params)
        cur.execute("""
            DELETE FROM native_load n
             WHERE n.ts >= %(a)s AND n.ts < %(b)s
               AND n.zone IN (SELECT DISTINCT zone FROM _stage_load)
               AND NOT EXISTS (SELECT 1 FROM _stage_load s WHERE s.zone = n.zone AND s.ts = n.ts)""", params)
        deleted = cur.rowcount
    return LoadCounts(len(long), inserted, updated, deleted)


# --- fuel mix ------------------------------------------------------------------------


def upsert_generation(conn, long: pd.DataFrame, shares: pd.DataFrame, year: int) -> LoadCounts:
    """Replace the `year` slice of generation_by_fuel.

    long: ts, fuel, gen_mw, settlement_type (parse.parse_fuel_mix).
    shares: wide fractions (parse.fuel_shares); fuels without a share (WSL) store NULL.
    """
    share_long = shares.rename_axis(index="ts", columns="fuel").stack().rename("share").reset_index()
    rows = long.merge(share_long, on=["ts", "fuel"], how="left", validate="one_to_one")
    _require_inside(rows["ts"], year, "fuel mix")
    a, b = year_window(year)
    params = {"a": a, "b": b}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _stage_fuel (ts timestamptz NOT NULL, fuel text NOT NULL, "
                    "gen_mw double precision NOT NULL, share double precision, settlement_type text, "
                    "PRIMARY KEY (ts, fuel)) ON COMMIT DROP")
        _copy(cur, "_stage_fuel", rows, ["ts", "fuel", "gen_mw", "share", "settlement_type"])
        cur.execute("ANALYZE _stage_fuel")
        inserted, updated = _upsert_counts(cur, """
            INSERT INTO generation_by_fuel AS g (ts, fuel, gen_mw, share, settlement_type)
            SELECT ts, fuel, gen_mw, share, settlement_type FROM _stage_fuel
            ON CONFLICT (ts, fuel) DO UPDATE
               SET gen_mw = EXCLUDED.gen_mw, share = EXCLUDED.share, settlement_type = EXCLUDED.settlement_type
             WHERE (g.gen_mw, g.share, g.settlement_type)
                   IS DISTINCT FROM (EXCLUDED.gen_mw, EXCLUDED.share, EXCLUDED.settlement_type)""", params)
        cur.execute("""
            DELETE FROM generation_by_fuel g
             WHERE g.ts >= %(a)s AND g.ts < %(b)s
               AND g.fuel IN (SELECT DISTINCT fuel FROM _stage_fuel)
               AND NOT EXISTS (SELECT 1 FROM _stage_fuel s WHERE s.fuel = g.fuel AND s.ts = g.ts)""", params)
        deleted = cur.rowcount
    return LoadCounts(len(rows), inserted, updated, deleted)


# --- provenance ----------------------------------------------------------------------


def record_file(conn, *, dataset: str, year: int, source_url: str, source_name: str,
                published_at: datetime | None, sha256: str, size: int, rows_loaded: int,
                download_s: float | None, parse_s: float | None, load_s: float | None) -> None:
    """Upsert the provenance row for (dataset, year)."""
    conn.execute("""
        INSERT INTO ingest_files (dataset, year, source_url, source_name, published_at, sha256, bytes,
                                  rows_loaded, download_s, parse_s, load_s, ingested_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (dataset, year) DO UPDATE SET
            source_url = EXCLUDED.source_url, source_name = EXCLUDED.source_name,
            published_at = EXCLUDED.published_at, sha256 = EXCLUDED.sha256, bytes = EXCLUDED.bytes,
            rows_loaded = EXCLUDED.rows_loaded, download_s = EXCLUDED.download_s, parse_s = EXCLUDED.parse_s,
            load_s = EXCLUDED.load_s, ingested_at = EXCLUDED.ingested_at""",
                 (dataset, year, source_url, source_name, published_at, sha256, size, rows_loaded,
                  download_s, parse_s, load_s))
