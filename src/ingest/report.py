"""Gap report and M1 self-check, computed from what is actually stored in Postgres.

    python -m src.ingest.report [--years 2024 2025] [--no-notes]

Prints a Markdown report, writes it into docs/notes/data.md between the
gap-report markers, and exits 1 unless every check passes:
- missing intervals < 0.1% for every (location, market, year), for native
  load and for the fuel mix;
- 0 duplicates (and nothing off the interval grid);
- the DST days have the right interval counts (23/25 hours);
- the fall-back repeated hour is placed consistently in load and fuel mix;
- where data/snapshot overlaps, Postgres returns the snapshot's numbers
  (the known differences are listed, see docs/notes/data.md).
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from src.ingest.store import TZ, year_window

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = REPO_ROOT / "docs" / "notes" / "data.md"
# The four DST days M1 checks: 2024-03-10, 2024-11-03, 2025-03-09, 2025-11-02 (dst_days computes them).
MAX_MISSING_PCT = 0.1  # M1 acceptance; config.yaml data.max_missing_pct overrides it
STEP_S = {"RT": 900, "DA": 3600}
DATASETS = ("native_load", "fuel_mix", "rt_spp", "da_spp")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    sections: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    max_missing_pct: float = MAX_MISSING_PCT

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def markdown(self) -> str:
        lines = [f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `python -m src.ingest.report`.", ""]
        lines += self.sections
        lines += ["### Self-check", ""]
        lines += [f"- **{'PASS' if c.ok else 'FAIL'}** {c.name}" + (f": {c.detail}" if c.detail else "") for c in self.checks]
        lines += ["", f"**Result: {'PASS' if self.ok else 'FAIL'}**", ""]
        return "\n".join(lines)


def dst_days(year: int) -> tuple[date, date]:
    """(spring-forward, fall-back) local dates of `year` in America/Chicago: the 23- and 25-hour days."""
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    hours = ((days + pd.Timedelta(days=1)).tz_localize(TZ) - days.tz_localize(TZ)) / pd.Timedelta(hours=1)
    spring, fall = days[hours == 23], days[hours == 25]
    if len(spring) != 1 or len(fall) != 1:
        raise ValueError(f"expected one 23-hour and one 25-hour day in {year}")
    return spring[0].date(), fall[0].date()


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    a = pd.Timestamp(day, tz=TZ).tz_convert("UTC").to_pydatetime()
    b = pd.Timestamp(day + timedelta(days=1), tz=TZ).tz_convert("UTC").to_pydatetime()
    return a, b


def expected_intervals(a: datetime, b: datetime, step_s: int) -> int:
    return int((b - a).total_seconds()) // step_s


def _fmt(x, digits=2) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x:,.{digits}f}"


def _q(conn, sql: str, params=None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        return cur.fetchall()


# --- sections ----------------------------------------------------------------------


def _prices_section(conn, years: list[int], markets: list[str], rep: Report) -> None:
    rows_out, worst_missing, dups, off_grid, missing_groups = [], 0.0, 0, 0, []
    for year in years:
        a, b = year_window(year)
        for market in markets:
            step = STEP_S[market]
            stats = _q(conn, """
                SELECT location, count(*), count(DISTINCT ts),
                       count(*) FILTER (WHERE mod(extract(epoch FROM ts)::bigint, %(step)s) <> 0),
                       min(price_usd_mwh), max(price_usd_mwh), avg(price_usd_mwh)
                  FROM prices WHERE market = %(m)s AND ts >= %(a)s AND ts < %(b)s
                 GROUP BY location ORDER BY location""", {"m": market, "a": a, "b": b, "step": step})
            expected = expected_intervals(a, b, step)
            if not stats:
                missing_groups.append(f"{market} {year}: no rows")
                continue
            for loc, n, n_distinct, n_off, pmin, pmax, pmean in stats:
                missing = expected - (n_distinct - n_off)
                pct = 100.0 * missing / expected
                worst_missing = max(worst_missing, pct)
                dups += n - n_distinct
                off_grid += n_off
                if pct >= rep.max_missing_pct:
                    missing_groups.append(f"{loc} {market} {year}: {pct:.3f}%")
                rows_out.append(f"| {loc} | {market} | {year} | {n:,} | {expected:,} | {missing:,} | {pct:.3f}% "
                                f"| {n - n_distinct} | {n_off} | {_fmt(pmin)} | {_fmt(pmax)} | {_fmt(pmean)} |")
    rep.sections += [
        "### Prices: rows, gaps and duplicates per (location, market, year)", "",
        "Expected = intervals in the ERCOT (America/Chicago) calendar year: 15-min for RT, hourly for DA. "
        "Duplicates = rows sharing (ts, location, market). Prices in $/MWh.", "",
        "| location | market | year | rows | expected | missing | missing % | duplicates | off-grid | min | max | mean |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        *rows_out, "",
    ]
    rep.checks.append(Check(f"prices: missing < {rep.max_missing_pct}% for every (location, market, year)",
                            not missing_groups and bool(rows_out),
                            f"worst {worst_missing:.3f}%" + (f"; failing: {', '.join(missing_groups)}" if missing_groups else "")))
    rep.checks.append(Check("prices: 0 duplicates and 0 off-grid timestamps", dups == 0 and off_grid == 0,
                            f"{dups} duplicates, {off_grid} off-grid"))


def _dst_section(conn, years: list[int], markets: list[str], datasets: list[str], rep: Report) -> None:
    days = [d for y in years for d in dst_days(y)]
    header = "| series | " + " | ".join(f"{d} ({'spring' if d.month < 6 else 'fall'})" for d in days) + " |"
    lines = [header, "|---|" + "---:|" * len(days)]
    bad = []

    def row(label: str, counts_by_day: list[tuple[set[int], int]]) -> None:
        cells = []
        for d, (counts, expected) in zip(days, counts_by_day):
            got = ", ".join(str(c) for c in sorted(counts)) or "0"
            ok = counts == {expected}
            cells.append(f"{got} (want {expected}){'' if ok else ' FAIL'}")
            if not ok:
                bad.append(f"{label} {d}: {got}, want {expected}")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    for market in markets:
        per_hour = 3600 // STEP_S[market]
        first, last = year_window(min(years))[0], year_window(max(years))[1]
        locations = [r[0] for r in _q(conn, "SELECT DISTINCT location FROM prices WHERE market = %(m)s "
                                            "AND ts >= %(a)s AND ts < %(b)s", {"m": market, "a": first, "b": last})]
        counts = []
        for d in days:
            a, b = local_day_bounds(d)
            got = dict(_q(conn, "SELECT location, count(*) FROM prices WHERE market = %(m)s AND ts >= %(a)s "
                                "AND ts < %(b)s GROUP BY location", {"m": market, "a": a, "b": b}))
            per_location = {got.get(loc, 0) for loc in locations}  # a location with no rows that day counts as 0
            counts.append((per_location, expected_intervals(a, b, 3600) * per_hour))
        row(f"{market} prices (each of {len(locations)} locations)", counts)
    if "native_load" in datasets:
        counts = []
        for d in days:
            a, b = local_day_bounds(d)
            n = _q(conn, "SELECT count(*) FROM native_load WHERE zone = 'ERCOT' AND ts >= %(a)s AND ts < %(b)s",
                   {"a": a, "b": b})[0][0]
            counts.append(({n}, expected_intervals(a, b, 3600)))
        row("native load (ERCOT, hourly)", counts)
    if "fuel_mix" in datasets:
        counts = []
        for d in days:
            a, b = local_day_bounds(d)
            n = _q(conn, "SELECT count(DISTINCT ts) FROM generation_by_fuel WHERE ts >= %(a)s AND ts < %(b)s",
                   {"a": a, "b": b})[0][0]
            counts.append(({n}, expected_intervals(a, b, 900)))
        row("fuel mix (15-min)", counts)
    rep.sections += ["### DST days: intervals per local (America/Chicago) day", "",
                     "Spring-forward days have 23 hours (92 x 15-min), fall-back days 25 (100 x 15-min).", "",
                     *lines, ""]
    rep.checks.append(Check("DST days have the right interval counts", not bad, "; ".join(bad)))


def _load_section(conn, years: list[int], rep: Report) -> None:
    lines = ["| year | hours | expected | missing | missing % | duplicates | off-grid | zones | min MW | max MW | mean MW |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    bad, dup_total = [], 0
    for year in years:
        a, b = year_window(year)
        n, n_distinct, n_off, lo, hi, mean = _q(conn, """
            SELECT count(*), count(DISTINCT ts), count(*) FILTER (WHERE mod(extract(epoch FROM ts)::bigint, 3600) <> 0),
                   min(load_mw), max(load_mw), avg(load_mw)
              FROM native_load WHERE zone = 'ERCOT' AND ts >= %(a)s AND ts < %(b)s""", {"a": a, "b": b})[0]
        zones = _q(conn, "SELECT count(DISTINCT zone) FROM native_load WHERE ts >= %(a)s AND ts < %(b)s",
                   {"a": a, "b": b})[0][0]
        expected = expected_intervals(a, b, 3600)
        missing = expected - (n_distinct - n_off)
        pct = 100.0 * missing / expected
        dup_total += (n - n_distinct) + n_off
        if pct >= rep.max_missing_pct:
            bad.append(f"{year}: {pct:.3f}%")
        lines.append(f"| {year} | {n:,} | {expected:,} | {missing:,} | {pct:.3f}% | {n - n_distinct} | {n_off} | {zones} "
                     f"| {_fmt(lo, 0)} | {_fmt(hi, 0)} | {_fmt(mean, 0)} |")
    rep.sections += ["### Native load (ERCOT total, hourly)", "", *lines, ""]
    rep.checks.append(Check(f"native load: missing < {rep.max_missing_pct}%, 0 duplicates", not bad and dup_total == 0,
                            "; ".join(bad) or f"{dup_total} duplicates/off-grid"))


def _fuel_section(conn, years: list[int], rep: Report) -> None:
    lines = ["| year | intervals | expected | missing | missing % | duplicates | off-grid | fuels | max abs(sum(shares) - 1) |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    bad, dup_total = [], 0
    for year in years:
        a, b = year_window(year)
        n_ts, n_off, fuels = _q(conn, """
            SELECT count(DISTINCT ts), count(DISTINCT ts) FILTER (WHERE mod(extract(epoch FROM ts)::bigint, 900) <> 0),
                   count(DISTINCT fuel)
              FROM generation_by_fuel WHERE ts >= %(a)s AND ts < %(b)s""", {"a": a, "b": b})[0]
        dups, err = _q(conn, """
            SELECT count(*) FILTER (WHERE n <> nf), max(abs(s - 1))
              FROM (SELECT ts, count(*) AS n, count(DISTINCT fuel) AS nf, sum(share) AS s
                      FROM generation_by_fuel WHERE ts >= %(a)s AND ts < %(b)s GROUP BY ts) t""", {"a": a, "b": b})[0]
        expected = expected_intervals(a, b, 900)
        missing = expected - (n_ts - n_off)
        pct = 100.0 * missing / expected
        dup_total += (dups or 0) + n_off
        if pct >= rep.max_missing_pct or (err is not None and err > 1e-9):
            bad.append(f"{year}: missing {pct:.3f}%, share-sum error {err}")
        lines.append(f"| {year} | {n_ts:,} | {expected:,} | {missing:,} | {pct:.3f}% | {dups or 0} | {n_off} | {fuels} "
                     f"| {err if err is None else f'{err:.1e}'} |")
    rep.sections += ["### Fuel mix (15-min generation by fuel)", "",
                     "Shares are fractions of positive generation with WSL excluded; they must sum to 1.", "",
                     *lines, ""]
    rep.checks.append(Check(f"fuel mix: missing < {rep.max_missing_pct}%, 0 duplicates, shares sum to 1",
                            not bad and dup_total == 0, "; ".join(bad) or f"{dup_total} duplicates/off-grid"))


def _context_section(conn, years: list[int], rep: Report) -> None:
    lines = ["| year | RT rows | with demand_mw | with fuel_mix |", "|---|---:|---:|---:|"]
    for year in years:
        a, b = year_window(year)
        n, nd, nf = _q(conn, "SELECT count(*), count(demand_mw), count(fuel_mix) FROM prices "
                             "WHERE market = 'RT' AND ts >= %(a)s AND ts < %(b)s", {"a": a, "b": b})[0]
        lines.append(f"| {year} | {n:,} | {nd:,} | {nf:,} |")
    rep.sections += ["### Context on RT rows (what the replay feed puts in PriceTick.demand_mw / fuel_mix)", "",
                     *lines, ""]


def fallback_hour_evidence(conn, day: date) -> dict | None:
    """How well the stored placement of the fall-back repeated hour fits, versus the two passes swapped.

    Two independent tests over local 00:00 CDT to 03:00 CST (4 clock hours, 16
    fuel-mix intervals):
    - ratio misfit: r = ERCOT load / total generation for each hour. Load and
      fuel come from different files, so a pass misplaced in either one bends
      r. The misfit is how far r of the two repeated hours (01:00 CDT, 01:00
      CST) lies from the straight line between the unambiguous hours around
      them (00:00 CDT, 02:00 CST). Smaller is more consistent.
    - wind jumps: sum of |step| of 15-min wind output across the window. Wind
      is physically smooth, so the right order has the smaller sum.
    """
    a = pd.Timestamp(day, tz=TZ).tz_convert("UTC").to_pydatetime()
    b = a + timedelta(hours=4)
    gen = _q(conn, "SELECT ts, sum(gen_mw), sum(gen_mw) FILTER (WHERE fuel = 'Wind') FROM generation_by_fuel "
                   "WHERE ts >= %(a)s AND ts < %(b)s GROUP BY ts ORDER BY ts", {"a": a, "b": b})
    load = _q(conn, "SELECT ts, load_mw FROM native_load WHERE zone = 'ERCOT' AND ts >= %(a)s AND ts < %(b)s "
                    "ORDER BY ts", {"a": a, "b": b})
    if len(gen) != 16 or len(load) != 4:
        return None
    total = np.array([g[1] for g in gen]).reshape(4, 4).mean(axis=1)
    wind = np.array([g[2] for g in gen])
    load_mw = np.array([x[1] for x in load])
    swap_hours = [0, 2, 1, 3]
    swap_intervals = list(range(4)) + list(range(8, 12)) + list(range(4, 8)) + list(range(12, 16))

    def misfit(l, g):
        r = l / g
        line = r[0] + (r[3] - r[0]) * np.array([1 / 3, 2 / 3])
        return float(np.abs(r[1:3] - line).sum())

    return {
        "ratio_misfit": misfit(load_mw, total),
        "ratio_misfit_swapped": misfit(load_mw, total[swap_hours]),
        "wind_jumps": float(np.abs(np.diff(wind)).sum()),
        "wind_jumps_swapped": float(np.abs(np.diff(wind[swap_intervals])).sum()),
    }


def _fallback_section(conn, years: list[int], rep: Report) -> None:
    lines = ["| fall-back day | load/gen ratio misfit: stored | swapped | wind jumps MW: stored | swapped | verdict |",
             "|---|---:|---:|---:|---:|---|"]
    bad, seen = [], 0
    for year in years:
        day = dst_days(year)[1]
        ev = fallback_hour_evidence(conn, day)
        if ev is None:
            lines.append(f"| {day} | n/a | n/a | n/a | n/a | load or fuel mix missing |")
            continue
        seen += 1
        ok = ev["ratio_misfit"] < ev["ratio_misfit_swapped"] and ev["wind_jumps"] < ev["wind_jumps_swapped"]
        if not ok:
            bad.append(str(day))
        lines.append(f"| {day} | {ev['ratio_misfit']:.4f} | {ev['ratio_misfit_swapped']:.4f} | {ev['wind_jumps']:,.0f} "
                     f"| {ev['wind_jumps_swapped']:,.0f} | {'stored order fits better' if ok else 'SWAPPED ORDER FITS BETTER'} |")
    rep.sections += [
        "### Fall-back hour placement (DST cross-check)", "",
        "Native load flags the repeated hour as `HH:00 DST`, the fuel mix as `(DST)` columns. Both are stored as the "
        "second, CST pass (the MIS Repeated Hour Flag convention). The stored order must fit better than the two "
        "passes swapped on both tests.", "",
        *lines, ""]
    rep.checks.append(Check("fall-back repeated hour placed consistently in load and fuel mix",
                            not bad and seen > 0, "; ".join(bad) or f"{seen} fall-back day(s) checked"))


def _snapshot_section(conn, years: list[int], datasets: list[str], rep: Report) -> None:
    """Postgres (series_pg) against data/snapshot (series, snapshot backend) wherever both have data."""
    from src.data import series, series_pg

    snap_dir = series.SNAPSHOT_DIR
    if not snap_dir.exists():
        rep.sections += ["### Snapshot parity", "", "data/snapshot is not present here (container); skipped.", ""]
        return
    lines = ["| series | year | rows compared | result |", "|---|---|---:|---|"]
    bad = []
    old = os.environ.get("SERIES_BACKEND")
    os.environ["SERIES_BACKEND"] = "snapshot"
    try:
        for year in years:
            a, b = year_window(year)
            for market, fn_pg, fn_snap, ds in (("RT", series_pg.rt_prices, series.rt_prices, "rt_spp"),
                                               ("DA", series_pg.da_prices, series.da_prices, "da_spp")):
                path = snap_dir / f"ercot_{market.lower()}_{year}_hubs.parquet"
                if ds not in datasets or not path.exists():
                    continue
                locs = sorted(pd.read_parquet(path, columns=["location"])["location"].unique())
                n, diffs = 0, []
                for loc in locs:
                    s, p = fn_snap(loc, a, b), fn_pg(loc, a, b, conn=conn)
                    n += len(s)
                    if not (s.index.equals(p.index) and np.array_equal(s.to_numpy(), p.to_numpy())):
                        diffs.append(loc)
                result = "identical" if not diffs else f"DIFFERENT: {', '.join(diffs)}"
                if diffs:
                    bad.append(f"{market} {year}")
                lines.append(f"| {market} prices, {len(locs)} locations | {year} | {n:,} | {result} |")
            if "native_load" in datasets and (snap_dir / f"ercot_hourly_native_load_{year}.parquet").exists():
                s, p = series.system_load(a, b), series_pg.system_load(a, b, conn=conn)
                result, ok = describe_load_diff(s, p)
                if not ok:
                    bad.append(f"load {year}")
                lines.append(f"| native load, ERCOT | {year} | {len(s):,} | {result} |")
            if "fuel_mix" in datasets and (snap_dir / f"ercot_fuel_mix_15min_{year}.parquet").exists():
                s, p = series.fuel_mix(a, b), series_pg.fuel_mix(a, b, conn=conn)
                ok = s.index.equals(p.index) and list(s.columns) == list(p.columns) and np.array_equal(s.to_numpy(), p.to_numpy())
                if not ok:
                    bad.append(f"fuel mix {year}")
                lines.append(f"| fuel mix shares | {year} | {s.size:,} | {'identical' if ok else 'DIFFERENT'} |")
    finally:
        if old is None:
            os.environ.pop("SERIES_BACKEND", None)
        else:
            os.environ["SERIES_BACKEND"] = old
    rep.sections += ["### Snapshot parity (Postgres vs data/snapshot, same series API)", "", *lines, ""]
    rep.checks.append(Check("Postgres matches data/snapshot where they overlap (known differences excepted)",
                            not bad, "; ".join(bad)))


def describe_load_diff(snap: pd.Series, pg: pd.Series) -> tuple[str, bool]:
    """Compare system_load from the two backends. Returns (description, acceptable).

    Two differences are known and accepted:
    - The snapshot (built with gridstatus) has the fall-back repeated hour's two
      values swapped. The ingest keeps ERCOT's order (see docs/notes/data.md).
    - A few values differ in the last bit: the workbook stores 17 significant
      digits (e.g. 61388.367209000004), calamine keeps them, and the snapshot
      holds the value rounded to 6 decimals.
    """
    if not snap.index.equals(pg.index):
        return "DIFFERENT index", False
    s, p = snap.to_numpy(), pg.to_numpy()
    differ = np.flatnonzero(s != p)
    swapped = [i for i in differ if i + 1 < len(s) and s[i] == p[i + 1] and s[i + 1] == p[i]]
    swap_rows = set(swapped) | {i + 1 for i in swapped}
    ulp = [i for i in differ if i not in swap_rows and abs(s[i] - p[i]) <= 4 * np.spacing(abs(s[i]))]
    other = [i for i in differ if i not in swap_rows and i not in ulp]
    fallback_hours = {str(snap.index[i]) for i in swap_rows}
    parts = ["identical"] if len(differ) == 0 else []
    if swap_rows:
        parts.append(f"{len(swap_rows)} values swapped at {', '.join(sorted(fallback_hours))} (snapshot's fall-back hour order)")
    if ulp:
        parts.append(f"{len(ulp)} values differ in the last bit (snapshot rounded)")
    if other:
        parts.append(f"{len(other)} OTHER DIFFERENCES, e.g. {snap.index[other[0]]}")
    ok = not other and all(pd.Timestamp(snap.index[i]).tz_convert(TZ).month == 11 for i in swap_rows)
    return "; ".join(parts), ok


def _sources_section(conn, years: list[int], rep: Report) -> None:
    rows = _q(conn, """
        SELECT dataset, year, source_name, published_at, bytes, rows_loaded, download_s, parse_s, load_s, ingested_at
          FROM ingest_files WHERE year = ANY(%(y)s) ORDER BY year, dataset""", {"y": list(years)})
    lines = ["| dataset | year | file | published | MB | rows loaded | download s | parse s | load s | ingested (UTC) |",
             "|---|---|---|---|---:|---:|---:|---:|---:|---|"]
    for ds, year, name, pub, size, n, dl, ps, ld, at in rows:
        pub_s = pub.astimezone(timezone.utc).strftime("%Y-%m-%d") if pub else "n/a"
        lines.append(f"| {ds} | {year} | {name} | {pub_s} | {size / 1e6:.1f} | {n:,} | {_fmt(dl, 1)} | {_fmt(ps, 1)} "
                     f"| {_fmt(ld, 1)} | {at.astimezone(timezone.utc):%Y-%m-%d %H:%M} |")
    rep.sections += ["### Source files and timings (last load of each file)", "", *lines, ""]


def build(conn, years: list[int], datasets: list[str] | None = None, snapshot: bool = True,
          max_missing_pct: float | None = None) -> Report:
    """The gap report and self-check for `years` over the stored data.

    snapshot=False skips the comparison with data/snapshot (for synthetic test data).
    max_missing_pct defaults to config.yaml data.max_missing_pct (0.1).
    """
    if max_missing_pct is None:
        from src.config import load_raw_config

        max_missing_pct = float(load_raw_config().get("data", {}).get("max_missing_pct", MAX_MISSING_PCT))
    datasets = list(datasets or DATASETS)
    markets = [m for m, ds in (("RT", "rt_spp"), ("DA", "da_spp")) if ds in datasets]
    rep = Report(max_missing_pct=max_missing_pct)
    if markets:
        _prices_section(conn, years, markets, rep)
    _dst_section(conn, years, markets, datasets, rep)
    if "native_load" in datasets:
        _load_section(conn, years, rep)
    if "fuel_mix" in datasets:
        _fuel_section(conn, years, rep)
    if "rt_spp" in datasets:
        _context_section(conn, years, rep)
    if "native_load" in datasets and "fuel_mix" in datasets:
        _fallback_section(conn, years, rep)
    if snapshot:
        _snapshot_section(conn, years, datasets, rep)
    _sources_section(conn, years, rep)
    return rep


# --- notes ---------------------------------------------------------------------------


def write_notes_section(markdown: str, marker: str, path: Path = NOTES_PATH) -> bool:
    """Replace the text between <!-- BEGIN marker --> and <!-- END marker --> in `path`.

    Appends the block when the markers are missing. Returns False (and writes
    nothing) when the notes directory does not exist, e.g. in the container.
    """
    if not path.parent.is_dir():
        return False
    begin, end = f"<!-- BEGIN {marker} -->", f"<!-- END {marker} -->"
    block = f"{begin}\n{markdown.rstrip()}\n{end}"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if begin in text and end in text:
        head, rest = text.split(begin, 1)
        _, tail = rest.split(end, 1)
        text = head + block + tail
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
    path.write_text(text, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    from src.config import load_raw_config
    from src.db import connect

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=int, nargs="+", help="ERCOT years (default: config data.years)")
    ap.add_argument("--datasets", nargs="+", choices=DATASETS, help="datasets to check (default: all)")
    ap.add_argument("--no-notes", action="store_true", help="do not write docs/notes/data.md")
    args = ap.parse_args(argv)
    years = args.years or list(load_raw_config().get("data", {}).get("years", [2024, 2025]))
    with connect(autocommit=True) as conn:
        rep = build(conn, years, args.datasets)
    md = rep.markdown()
    print(md)
    if not args.no_notes and write_notes_section(md, "gap-report"):
        print(f"(written to {NOTES_PATH.relative_to(REPO_ROOT)})")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
