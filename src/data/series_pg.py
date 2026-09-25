"""Postgres backend of src.data.series, with the same output as the snapshot backend.

It has the same functions and returns the same pandas objects: the index is
the UTC interval START (datetime64[ns, UTC]) with the same index name, the
series have the same names and dtypes (float64), and results are sorted and
sliced to [start, end). Naive bounds raise TypeError, as in the snapshot
backend. The tables are filled by `python -m src.ingest.ercot` (see
migrations/010_ingest_tables.sql). tests/test_series_parity.py proves that
the two backends agree wherever both have data.

Every function also takes an optional `conn` (an open psycopg connection).
Without one, it opens a short-lived connection through src.db.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

# Columns of series.fuel_mix: ERCOT's fuels without WSL, lowercased, in published (sorted) order.
FUEL_COLUMNS = ["biomass", "coal", "gas", "gas-cc", "hydro", "nuclear", "other", "solar", "wind"]
_EPOCH_US = "(extract(epoch FROM ts) * 1000000)::bigint"


def rt_prices(location: str, start: datetime, end: datetime, *, conn=None) -> pd.Series:
    """Real-time settlement point prices, native 15-min, $/MWh."""
    return _prices("RT", location, start, end, conn)


def da_prices(location: str, start: datetime, end: datetime, *, conn=None) -> pd.Series:
    """Day-ahead settlement point prices, hourly, $/MWh."""
    return _prices("DA", location, start, end, conn)


def system_load(start: datetime, end: datetime, *, conn=None) -> pd.Series:
    """ERCOT-wide native load, hourly, MW."""
    a, b = _bounds(start, end)
    rows = _fetch(conn, f"SELECT {_EPOCH_US}, load_mw FROM native_load "
                        "WHERE zone = 'ERCOT' AND ts >= %s AND ts < %s ORDER BY ts", (a, b))
    return _series(rows, name="ERCOT", index_name="Interval Start")


def fuel_mix(start: datetime, end: datetime, *, conn=None) -> pd.DataFrame:
    """Generation shares by fuel, 15-min. Fractions of positive generation;
    WSL (storage charging, negative) is excluded."""
    a, b = _bounds(start, end)
    rows = _fetch(conn, f"SELECT {_EPOCH_US}, fuel, share FROM generation_by_fuel "
                        "WHERE share IS NOT NULL AND ts >= %s AND ts < %s ORDER BY ts, fuel", (a, b))
    if not rows:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in FUEL_COLUMNS}, index=_index([], "ts"))
    us, fuel, share = zip(*rows)
    long = pd.DataFrame({"us": np.asarray(us, dtype="int64"), "fuel": fuel, "share": np.asarray(share, dtype="float64")})
    wide = long.pivot(index="us", columns="fuel", values="share")
    wide = wide[sorted(wide.columns)]
    out = pd.DataFrame(wide.to_numpy(dtype="float64"), index=_index(wide.index.to_numpy(), "ts"),
                       columns=[str(c).lower() for c in wide.columns])
    return out


# --- internals -------------------------------------------------------------------------


def _prices(market: str, location: str, start: datetime, end: datetime, conn) -> pd.Series:
    a, b = _bounds(start, end)
    rows = _fetch(conn, f"SELECT {_EPOCH_US}, price_usd_mwh FROM prices "
                        "WHERE location = %s AND market = %s AND ts >= %s AND ts < %s ORDER BY ts",
                  (location, market, a, b))
    return _series(rows, name=location, index_name="ts")


def _bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    # Same conversion as series._slice, so naive datetimes fail the same way (TypeError).
    return pd.Timestamp(start).tz_convert("UTC").to_pydatetime(), pd.Timestamp(end).tz_convert("UTC").to_pydatetime()


def _fetch(conn, sql: str, params: tuple) -> list[tuple]:
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    from src.db import connect

    with connect(autocommit=True) as own, own.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _index(epoch_us, name: str) -> pd.DatetimeIndex:
    us = np.asarray(epoch_us, dtype="int64")
    return pd.DatetimeIndex(pd.to_datetime(us, unit="us", utc=True), name=name).as_unit("ns")


def _series(rows: list[tuple], name: str, index_name: str) -> pd.Series:
    us = [r[0] for r in rows]
    values = np.asarray([r[1] for r in rows], dtype="float64")
    return pd.Series(values, index=_index(us, index_name), name=name)
