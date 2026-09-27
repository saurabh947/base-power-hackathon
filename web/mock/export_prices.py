"""Export the mock API's price fixture: REAL ERCOT prices, nothing simulated.

For every 2025 replay preset in config.yaml (presets.items), reads the real-time
15-minute settlement point prices of the fleet's load zones (config.yaml
fleet.locations: LZ_HOUSTON, LZ_NORTH, LZ_CPS, LZ_AEN) for the preset's local
date and the day after (America/Chicago) from the committed snapshot
(data/snapshot/ercot_rt_2025_hubs.parquet, ERCOT MIS report 13061) and writes
them to web/mock/data/ercot_rt_lz_preset_days_2025.json, one segment per preset.

It also exports the mock-only days in MOCK_ONLY: January 1-2, 2025 (the 2025
"From January 1" preset), three 2025 evenings Start no longer offers that the
mock's tests still replay (May 16, May 30 and July 11) and September 14-20, 2025. The mock has no 2026 data,
so its 2026 replays use 2025's real prices from the same dates, a year on, and
simulate the AI's prediction after Sep 20 from the week before (web/mock/sim.ts).
Presets of other years (2026 in config.yaml) are skipped: the snapshot is 2025.

Run from the repo root (after `make setup`):
    uv run python web/mock/export_prices.py        # or: make web-prices
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data" / "snapshot" / "ercot_rt_2025_hubs.parquet"
CONFIG = ROOT / "config.yaml"
OUT = Path(__file__).resolve().parent / "data" / "ercot_rt_lz_preset_days_2025.json"
TZ = "America/Chicago"
DAYS = 2  # the preset's day and the next, so every replay has room to run
YEAR = 2025  # the snapshot's year
# Days the mock needs beyond config.yaml's presets (see the module docstring).
MOCK_ONLY = [
    {"id": "jan1_2025", "date_local": "2025-01-01", "days": 2},
    # 2025 evenings no longer offered on Start that the mock's tests still replay (web/mock/sim.ts PRESETS)
    {"id": "spike_evening", "date_local": "2025-05-16"},
    {"id": "summer_spike", "date_local": "2025-07-11"},
    {"id": "ai_best_evening", "date_local": "2025-05-30"},
    {"id": "stand_in_2026_sep", "date_local": "2025-09-14", "days": 7},
]


def segment(df: pd.DataFrame, preset: dict, locations: list[str]) -> dict:
    start_local = pd.Timestamp(preset["date_local"]).tz_localize(TZ)
    end_local = start_local + pd.Timedelta(days=int(preset.get("days", DAYS)))
    start_utc, end_utc = start_local.tz_convert("UTC"), end_local.tz_convert("UTC")
    part = df[(df["ts"] >= start_utc) & (df["ts"] < end_utc)]
    wide = part.pivot(index="ts", columns="location", values="price_usd_mwh").sort_index()
    local = part.drop_duplicates("ts").set_index("ts")["interval_start_local"].sort_index()
    expected = int((end_utc - start_utc) / pd.Timedelta(minutes=15))
    missing = [loc for loc in locations if loc not in wide.columns]
    if missing or len(wide) != expected or wide[locations].isna().any().any():
        raise SystemExit(f"{preset['id']}: expected {expected} complete 15-min rows for {locations}, got {len(wide)} (missing {missing})")
    return {
        "preset": preset["id"],
        "local_dates": [(start_local + pd.Timedelta(days=d)).strftime("%Y-%m-%d") for d in range(int(preset.get("days", DAYS)))],
        "rows": [
            {
                "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ts_local": local[ts].isoformat(),
                **{loc: round(float(wide.at[ts, loc]), 2) for loc in locations},
            }
            for ts in wide.index
        ],
    }


def main() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    presets = [p for p in config["presets"]["items"] if str(p["date_local"]).startswith(f"{YEAR}-")]
    seen = {p["id"] for p in presets} | {str(p["date_local"]) for p in presets}
    presets += [m for m in MOCK_ONLY if m["id"] not in seen]
    locations = list(config["fleet"]["locations"])  # config order: the battery id blocks run in it
    df = pd.read_parquet(SRC)
    df = df[df["location"].isin(locations)]
    segments = [segment(df, p, locations) for p in presets]
    header = {
        "source_note": (
            "REAL DATA. ERCOT real-time settlement point prices (MIS report 13061, 15-minute "
            f"intervals), load zones {', '.join(locations)}, for each 2025 replay preset's Chicago-local "
            "date and the day after, plus the mock-only days (Jan 1-2, the May 16, May 30 and Jul 11 evenings, and "
            "Sep 14-20, 2025). Exported from "
            "data/snapshot/ercot_rt_2025_hubs.parquet by web/mock/export_prices.py. Everything else the "
            "mock API serves is simulated, including its 2026 prices (2025's, a year on) and prediction."
        ),
        "report_type_id": 13061,
        "market": "RT",
        "units": "USD/MWh",
        "interval_minutes": 15,
        "ts_note": "ts_utc is the interval start in UTC; ts_local is the same instant in America/Chicago.",
        "locations": locations,
    }
    # One row per line keeps the file small and its diffs readable.
    lines = ["{"]
    for key, value in header.items():
        lines.append(f" {json.dumps(key)}: {json.dumps(value)},")
    lines.append(' "segments": [')
    for i, seg in enumerate(segments):
        lines.append("  {")
        lines.append(f'   "preset": {json.dumps(seg["preset"])},')
        lines.append(f'   "local_dates": {json.dumps(seg["local_dates"])},')
        lines.append('   "rows": [')
        rows = [f"    {json.dumps(r, separators=(', ', ': '))}" for r in seg["rows"]]
        lines.append(",\n".join(rows))
        lines.append("   ]")
        lines.append("  }" + ("," if i < len(segments) - 1 else ""))
    lines.append(" ]")
    lines.append("}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json.loads(OUT.read_text(encoding="utf-8"))  # must stay valid JSON
    for seg in segments:
        peaks = {loc: max(r[loc] for r in seg["rows"]) for loc in locations}
        print(f"{seg['preset']}: {seg['local_dates']} {len(seg['rows'])} rows, peaks {peaks}")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
