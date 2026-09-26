"""Check the replay presets in config.yaml against real prices, and pick an "ordinary day".

    python -m src.ingest.presets [--year 2025] [--no-notes]

Prices come through src.data.series: Postgres when DATABASE_URL is set,
otherwise data/snapshot. For each preset and each fleet location, it prints
the peak RT price on the preset's local day, when the peak happens, and how
many 5-min ticks after start_utc it arrives. It then ranks the days of
`year` by how close their daily RT spread (max - min of the 15-min prices)
is to the year's median, for all fleet locations at once, and recommends the
closest one as ordinary_day. The result goes into docs/notes/data.md
between the presets markers.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from src.ingest.report import NOTES_PATH, REPO_ROOT, dst_days, write_notes_section

TZ = "America/Chicago"
TICK = timedelta(minutes=5)


def local_day_prices(location: str, day: date) -> pd.Series:
    from src.data import series

    a = pd.Timestamp(day, tz=TZ)
    return series.rt_prices(location, a.tz_convert("UTC"), (a + pd.Timedelta(days=1)).tz_convert("UTC"))


def check_presets(presets: list[dict], locations: list[str]) -> tuple[list[dict], str]:
    rows, lines = [], [
        "| preset | local date | replay starts (local) | location | peak $/MWh | peak interval (local) "
        "| ticks from start to peak | day min | day mean |",
        "|---|---|---|---|---:|---|---:|---:|---:|",
    ]
    for p in presets:
        day = date.fromisoformat(str(p["date_local"]))
        start = pd.Timestamp(str(p["start_utc"]).replace("Z", "+00:00")).tz_convert("UTC")
        for loc in locations:
            s = local_day_prices(loc, day)
            if s.empty:
                lines.append(f"| {p['id']} | {day} | {start.tz_convert(TZ):%H:%M %Z} | {loc} | no data | | | | |")
                continue
            peak_ts = s.idxmax()
            ticks = int((peak_ts - start) / TICK)
            row = {"preset": p["id"], "location": loc, "peak": float(s.max()), "peak_ts": peak_ts, "ticks": ticks,
                   "min": float(s.min()), "mean": float(s.mean())}
            rows.append(row)
            lines.append(f"| {p['id']} | {day} | {start.tz_convert(TZ):%H:%M %Z} | {loc} | {row['peak']:,.2f} "
                         f"| {peak_ts.tz_convert(TZ):%H:%M %Z} | {ticks} | {row['min']:,.2f} | {row['mean']:,.2f} |")
    return rows, "\n".join(lines)


def daily_stats(locations: list[str], year: int) -> pd.DataFrame:
    """One row per local day of `year`: spread, max and mean of each location's 15-min RT prices."""
    from src.data import series

    a = pd.Timestamp(f"{year}-01-01", tz=TZ).tz_convert("UTC")
    b = pd.Timestamp(f"{year + 1}-01-01", tz=TZ).tz_convert("UTC")
    frames = {}
    for loc in locations:
        s = series.rt_prices(loc, a, b)
        days = s.index.tz_convert(TZ).date
        g = s.groupby(days)
        frames[loc] = pd.DataFrame({"spread": g.max() - g.min(), "max": g.max(), "mean": g.mean(), "n": g.size()})
    return pd.concat(frames, axis=1)


def rank_ordinary_days(stats: pd.DataFrame, locations: list[str], year: int, top: int = 5) -> tuple[pd.DataFrame, dict]:
    """Days ranked by the worst relative distance of any location's spread from that location's median."""
    medians = {loc: float(stats[(loc, "spread")].median()) for loc in locations}
    full = pd.Series(True, index=stats.index)
    for loc in locations:
        full &= stats[(loc, "n")] == 96  # skips the 23/25-hour DST days and any day with gaps
    dst = set(dst_days(year))
    full &= pd.Series([d not in dst for d in stats.index], index=stats.index)
    dev = pd.concat([(stats[(loc, "spread")] - medians[loc]).abs() / medians[loc] for loc in locations], axis=1).max(axis=1)
    ranked = stats[full].assign(score=dev[full]).sort_values("score").head(top)
    return ranked, medians


def main(argv: list[str] | None = None) -> int:
    from src.config import load_config, load_raw_config
    from src.data import series

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--no-notes", action="store_true")
    args = ap.parse_args(argv)
    locations = list(load_config().fleet.locations)
    presets = list(load_raw_config().get("presets", {}).get("items", []))

    _, table = check_presets(presets, locations)
    stats = daily_stats(locations, args.year)
    ranked, medians = rank_ordinary_days(stats, locations, args.year)
    lines = [
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `python -m src.ingest.presets` "
        f"(series backend: {series.backend()}). RT 15-min settlement point prices; a peak's time is its interval start.",
        "",
        "#### Presets against real prices", "", table, "",
        f"#### Ordinary day: daily RT spread (max - min) closest to the {args.year} median", "",
        "Median daily spread: " + ", ".join(f"{loc} ${m:,.2f}/MWh" for loc, m in medians.items())
        + ". Score = the worst relative distance from the median over the fleet locations. "
          "DST days and days with gaps are skipped.", "",
        "| rank | local date | weekday | " + " | ".join(f"{loc} spread | {loc} max | {loc} mean" for loc in locations)
        + " | score |",
        "|---|---|---|" + "---:|---:|---:|" * len(locations) + "---:|",
    ]
    for i, (day, r) in enumerate(ranked.iterrows(), start=1):
        cells = " | ".join(f"{r[(loc, 'spread')]:,.2f} | {r[(loc, 'max')]:,.2f} | {r[(loc, 'mean')]:,.2f}" for loc in locations)
        lines.append(f"| {i} | {day} | {pd.Timestamp(day):%a} | {cells} | {float(r['score'].iloc[0]):.3f} |")
    best = ranked.index[0]
    start = pd.Timestamp(best, tz=TZ).tz_convert("UTC")
    lines += ["", f"Recommended ordinary_day: **{best}** (replay from local midnight = `{start:%Y-%m-%dT%H:%M:%SZ}`).", ""]
    md = "\n".join(lines)
    print(md)
    if not args.no_notes and write_notes_section(md, "presets"):
        print(f"(written to {NOTES_PATH.relative_to(REPO_ROOT)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
