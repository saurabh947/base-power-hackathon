"""Postgres loads, RT context and the gap report (src/ingest/store.py, report.py).

Every test runs in a throwaway schema (src.ingest.testdb.scratch_schema), so
real data is never touched. All prices, loads and MW values are SYNTHETIC
(made up for these tests, labelled as such). Skips when Postgres is not
reachable (set DATABASE_URL, e.g. from scripts/dev/services.sh up).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.feeds.replay import database_available
from src.ingest import parse, report, store
from src.ingest.testdb import scratch_schema

pytestmark = pytest.mark.postgres
UTC = timezone.utc


@pytest.fixture
def db():
    if not database_available():
        pytest.skip("Postgres not reachable: set DATABASE_URL (scripts/dev/services.sh up)")
    with scratch_schema() as conn:
        yield conn


def q(conn, sql, params=None):
    return conn.execute(sql, params or {}).fetchall()


def rt_frame(start: datetime, n: int, locations=("LZ_A", "LZ_B"), offset: float = 0.0) -> pd.DataFrame:
    """SYNTHETIC 15-min prices."""
    ts = pd.date_range(start, periods=n, freq="15min", tz="UTC").as_unit("ns")
    return pd.concat([pd.DataFrame({"ts": ts, "location": loc, "price_usd_mwh": np.arange(n) * 1.5 + k * 100 + offset})
                      for k, loc in enumerate(locations)], ignore_index=True)


T0 = datetime(2025, 6, 1, 5, tzinfo=UTC)  # 2025-06-01 00:00 CDT


def test_year_window_is_the_america_chicago_calendar_year():
    assert store.year_window(2025) == (datetime(2025, 1, 1, 6, tzinfo=UTC), datetime(2026, 1, 1, 6, tzinfo=UTC))
    assert report.expected_intervals(*store.year_window(2024), 900) == 35136
    assert report.expected_intervals(*store.year_window(2025), 3600) == 8760


def test_dst_days_are_the_four_m1_dates():
    assert [d.isoformat() for y in (2024, 2025) for d in report.dst_days(y)] == [
        "2024-03-10", "2024-11-03", "2025-03-09", "2025-11-02"]


def test_prices_upsert_is_idempotent(db):
    frame = rt_frame(T0, 8)
    first = store.upsert_prices(db, frame, "RT", 2025)
    assert (first.inserted, first.updated, first.deleted) == (16, 0, 0)
    again = store.upsert_prices(db, frame, "RT", 2025)
    assert (again.inserted, again.updated, again.deleted, again.unchanged) == (0, 0, 0, 16)
    assert q(db, "SELECT count(*) FROM prices")[0][0] == 16


def test_a_changed_price_updates_only_that_row(db):
    frame = rt_frame(T0, 8)
    store.upsert_prices(db, frame, "RT", 2025)
    frame.loc[3, "price_usd_mwh"] = 4999.99
    counts = store.upsert_prices(db, frame, "RT", 2025)
    assert (counts.inserted, counts.updated, counts.deleted) == (0, 1, 0)
    assert q(db, "SELECT price_usd_mwh FROM prices WHERE ts = %(t)s AND location = 'LZ_A'",
             {"t": frame.ts[3].to_pydatetime()})[0][0] == 4999.99


def test_stale_rows_of_the_slice_are_deleted_and_nothing_else(db):
    store.upsert_prices(db, rt_frame(T0, 8), "RT", 2025)
    store.upsert_prices(db, rt_frame(T0, 8), "DA", 2025)  # other market
    store.upsert_prices(db, rt_frame(datetime(2024, 6, 1, 5, tzinfo=UTC), 4), "RT", 2024)  # other year
    store.upsert_prices(db, rt_frame(T0, 4, locations=("HB_OTHER",)), "RT", 2025)  # other location
    # a corrected file: LZ_A/LZ_B now start one interval later (e.g. an old DST bug fixed)
    counts = store.upsert_prices(db, rt_frame(T0 + timedelta(minutes=15), 8), "RT", 2025)
    assert (counts.inserted, counts.deleted) == (2, 2)
    got = dict(q(db, "SELECT market || ' ' || location, count(*) FROM prices GROUP BY 1"))
    assert got == {"RT LZ_A": 8 + 4, "RT LZ_B": 8 + 4, "DA LZ_A": 8, "DA LZ_B": 8, "RT HB_OTHER": 4}  # +4 = 2024 rows
    assert q(db, "SELECT min(ts) FROM prices WHERE market = 'RT' AND location = 'LZ_A' AND ts >= %(t)s",
             {"t": T0 - timedelta(days=1)})[0][0] == T0 + timedelta(minutes=15)


def test_rows_outside_the_year_are_rejected(db):
    with pytest.raises(ValueError, match="outside ERCOT year 2025"):
        store.upsert_prices(db, rt_frame(datetime(2026, 1, 1, 5, 45, tzinfo=UTC), 2), "RT", 2025)
    assert q(db, "SELECT count(*) FROM prices")[0][0] == 0  # nothing half-written


def test_values_round_trip_bit_for_bit(db):
    values = [61388.367209000004, 0.1, 1 / 3, -32.95, 5000.0, 2.220446049250313e-16, -0.0, 1e-300]
    ts = pd.date_range(T0, periods=len(values), freq="15min", tz="UTC")
    store.upsert_prices(db, pd.DataFrame({"ts": ts, "location": "LZ_A", "price_usd_mwh": values}), "RT", 2025)
    back = [r[0] for r in q(db, "SELECT price_usd_mwh FROM prices ORDER BY ts")]
    assert back == values and all(np.float64(a).tobytes() == np.float64(b).tobytes()
                                  for a, b in zip(back, values) if a != 0)


def load_and_fuel(conn, start: datetime, hours: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """SYNTHETIC hourly load and 15-min fuel mix covering `hours` hours from `start`, loaded into the db."""
    idx = pd.date_range(start, periods=hours, freq="h", tz="UTC", name="ts").as_unit("ns")
    load = pd.DataFrame({"COAST": 10000.0 + np.arange(hours), "ERCOT": 50000.0 + 10 * np.arange(hours)}, index=idx)
    store.upsert_native_load(conn, load, 2025)
    ts = pd.date_range(start, periods=4 * hours, freq="15min", tz="UTC").as_unit("ns")
    long = pd.concat([pd.DataFrame({"ts": ts, "fuel": fuel, "gen_mw": base + np.arange(len(ts)), "settlement_type": "FINAL"})
                      for fuel, base in (("Gas", 30000.0), ("Wind", 15000.0), ("WSL", -700.0))], ignore_index=True)
    long = long.sort_values(["ts", "fuel"]).reset_index(drop=True)
    shares = parse.fuel_shares(parse.fuel_mw_wide(long))
    store.upsert_generation(conn, long, shares, 2025)
    return load, shares


def test_rt_rows_carry_demand_and_fuel_mix_context_da_rows_do_not(db):
    load, shares = load_and_fuel(db, T0, 3)
    store.upsert_prices(db, rt_frame(T0, 12, locations=("LZ_A",)), "RT", 2025)
    store.upsert_prices(db, rt_frame(T0, 3, locations=("LZ_A",)), "DA", 2025)
    rows = q(db, "SELECT ts, demand_mw, fuel_mix FROM prices WHERE market = 'RT' ORDER BY ts")
    assert len(rows) == 12
    for ts, demand, mix in rows:
        hour = pd.Timestamp(ts).floor("h")
        assert demand == load.loc[hour, "ERCOT"]  # the hour containing the interval
        want = {k.lower(): v for k, v in shares.loc[pd.Timestamp(ts)].items()}
        assert mix == want and set(mix) == {"gas", "wind"}  # exact floats, WSL excluded
    assert q(db, "SELECT count(*) FROM prices WHERE market = 'DA' AND (demand_mw IS NOT NULL OR fuel_mix IS NOT NULL)")[0][0] == 0
    assert store.enrich_rt(db, 2025) == 0  # already consistent: nothing rewritten


def test_enrich_rt_backfills_context_loaded_after_the_prices(db):
    store.upsert_prices(db, rt_frame(T0, 8, locations=("LZ_A",)), "RT", 2025)
    assert q(db, "SELECT count(demand_mw) FROM prices")[0][0] == 0
    load_and_fuel(db, T0, 1)  # covers the first 4 intervals only
    assert store.enrich_rt(db, 2025) == 4
    assert q(db, "SELECT count(demand_mw), count(fuel_mix) FROM prices")[0] == (4, 4)
    assert store.enrich_rt(db, 2025) == 0


def test_native_load_and_generation_are_idempotent(db):
    load_and_fuel(db, T0, 2)
    idx = pd.date_range(T0, periods=2, freq="h", tz="UTC", name="ts").as_unit("ns")
    again = store.upsert_native_load(db, pd.DataFrame({"COAST": [10000.0, 10001.0], "ERCOT": [50000.0, 50010.0]}, index=idx), 2025)
    assert (again.inserted, again.updated, again.deleted, again.unchanged) == (0, 0, 0, 4)
    rows = q(db, "SELECT fuel, share FROM generation_by_fuel WHERE ts = %(t)s ORDER BY fuel", {"t": T0})
    assert rows[0][0] == "Gas" and rows[2] == ("WSL", None) and abs(rows[0][1] + rows[1][1] - 1) < 1e-15


def test_record_file_keeps_the_latest_provenance(db):
    kw = dict(dataset="rt_spp", year=2025, source_url="https://example.test/a", source_name="a.zip",
              published_at=None, sha256="0" * 64, size=10, rows_loaded=5, download_s=1.0, parse_s=2.0, load_s=3.0)
    store.record_file(db, **kw)
    store.record_file(db, **{**kw, "source_name": "b.zip", "rows_loaded": 7})
    assert q(db, "SELECT source_name, rows_loaded FROM ingest_files") == [("b.zip", 7)]


# --- gap report on a complete SYNTHETIC year -------------------------------------------------


def synthetic_year(conn, year: int = 2025) -> None:
    """A complete SYNTHETIC ERCOT year: 1 location RT + DA, ERCOT load, 3-fuel mix. Smooth in UTC time."""
    a, b = store.year_window(year)
    q15 = pd.date_range(a, b, freq="15min", tz="UTC", inclusive="left").as_unit("ns")
    hours = pd.date_range(a, b, freq="h", tz="UTC", inclusive="left", name="ts").as_unit("ns")
    store.upsert_native_load(conn, pd.DataFrame({"ERCOT": 60000.0 - 5.0 * np.arange(len(hours))}, index=hours), year)
    j = np.arange(len(q15))
    long = pd.concat([pd.DataFrame({"ts": q15, "fuel": f, "gen_mw": v, "settlement_type": "FINAL"}) for f, v in
                      (("Gas", 40000.0 - 0.5 * j), ("Wind", 20000.0 - 0.25 * j), ("WSL", np.full(len(j), -100.0)))])
    long = long.sort_values(["ts", "fuel"]).reset_index(drop=True)
    store.upsert_generation(conn, long, parse.fuel_shares(parse.fuel_mw_wide(long)), year)
    store.upsert_prices(conn, pd.DataFrame({"ts": q15, "location": "LZ_TEST", "price_usd_mwh": 20 + (j % 96) * 0.5}), "RT", year)
    store.upsert_prices(conn, pd.DataFrame({"ts": hours, "location": "LZ_TEST", "price_usd_mwh": 25.0}), "DA", year)


def test_report_passes_on_a_complete_year(db):
    synthetic_year(db)
    rep = report.build(db, [2025], snapshot=False)
    assert rep.ok, rep.markdown()
    md = rep.markdown()
    assert "| LZ_TEST | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 |" in md
    assert "| RT prices (each of 1 locations) | 92 (want 92) | 100 (want 100) |" in md
    assert "stored order fits better" in md and "**Result: PASS**" in md


def test_report_fails_on_gaps_and_wrong_dst_counts(db):
    synthetic_year(db)
    db.execute("DELETE FROM prices WHERE market = 'RT' AND ts >= '2025-11-02 06:00+00' AND ts < '2025-11-02 07:00+00'")
    db.execute("DELETE FROM prices WHERE market = 'RT' AND ts >= '2025-07-01 00:00+00' AND ts < '2025-07-01 10:00+00'")
    rep = report.build(db, [2025], snapshot=False)
    failed = {c.name for c in rep.checks if not c.ok}
    assert any(n.startswith("prices: missing") for n in failed)  # 44 of 35,040 = 0.126%
    assert "DST days have the right interval counts" in failed
    assert "**Result: FAIL**" in rep.markdown()


def test_report_counts_a_location_missing_a_whole_dst_day_as_zero(db):
    synthetic_year(db)
    db.execute("""INSERT INTO prices (ts, location, market, price_usd_mwh)
                  SELECT ts, 'LZ_TWO', market, price_usd_mwh FROM prices
                   WHERE market = 'RT' AND NOT (ts >= '2025-11-02 05:00+00' AND ts < '2025-11-03 06:00+00')""")
    rep = report.build(db, [2025], datasets=["rt_spp"], snapshot=False)
    dst = next(c for c in rep.checks if c.name == "DST days have the right interval counts")
    assert not dst.ok and "2025-11-02: 0, 100, want 100" in dst.detail
    assert "| RT prices (each of 2 locations) |" in rep.markdown()


def test_report_without_a_snapshot_dir_skips_parity(db, monkeypatch, tmp_path):
    from src.data import series

    synthetic_year(db)
    monkeypatch.setattr(series, "SNAPSHOT_DIR", tmp_path / "absent")  # as in the Cloud Run image
    rep = report.build(db, [2025])
    assert rep.ok, rep.markdown()
    assert "data/snapshot is not present here (container); skipped." in rep.markdown()


def test_report_catches_a_swapped_fall_back_hour(db):
    synthetic_year(db)
    # swap the two passes of the repeated hour in the fuel mix, as a wrong DST parse would
    # (three moves: primary keys are checked row by row, so a one-statement swap collides)
    # (offsets in hours: '100 days' would be applied in the session time zone, across a DST change)
    for sql in ("UPDATE generation_by_fuel SET ts = ts + interval '2400 hours' "
                "WHERE ts >= '2025-11-02 06:00+00' AND ts < '2025-11-02 07:00+00'",
                "UPDATE generation_by_fuel SET ts = ts - interval '1 hour' "
                "WHERE ts >= '2025-11-02 07:00+00' AND ts < '2025-11-02 08:00+00'",
                "UPDATE generation_by_fuel SET ts = ts - interval '2399 hours' "
                "WHERE ts >= '2026-02-10 06:00+00' AND ts < '2026-02-10 07:00+00'"):
        db.execute(sql)
    assert db.execute("SELECT count(*) FROM generation_by_fuel WHERE ts >= '2026-01-01 06:00+00'").fetchone()[0] == 0
    rep = report.build(db, [2025], datasets=["native_load", "fuel_mix"], snapshot=False)
    bad = [c for c in rep.checks if not c.ok]
    assert [c.name for c in bad] == ["fall-back repeated hour placed consistently in load and fuel mix"]


def test_notes_section_is_replaced_between_markers(tmp_path):
    notes = tmp_path / "data.md"
    notes.write_text("# Notes\n\nintro\n\n<!-- BEGIN gap-report -->\nold\n<!-- END gap-report -->\n\ntail\n")
    assert report.write_notes_section("new report", "gap-report", notes)
    assert notes.read_text() == "# Notes\n\nintro\n\n<!-- BEGIN gap-report -->\nnew report\n<!-- END gap-report -->\n\ntail\n"
    assert report.write_notes_section("t", "timings", notes)
    assert notes.read_text().endswith("<!-- BEGIN timings -->\nt\n<!-- END timings -->\n")
    assert not report.write_notes_section("x", "gap-report", tmp_path / "missing" / "data.md")
    assert not (tmp_path / "missing").exists()  # the container case: nothing is created
