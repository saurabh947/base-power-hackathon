"""Export the mock API's price fixture: REAL ERCOT prices, nothing simulated.

For every replay preset in config.yaml (presets.items), reads the LZ_HOUSTON and
LZ_NORTH real-time 15-minute settlement point prices for the preset's local date
and the day after (America/Chicago) from the committed snapshot
(data/snapshot/ercot_rt_2025_hubs.parquet, ERCOT MIS report 13061) and writes
them to web/mock/data/ercot_rt_lz_preset_days_2025.json, one segment per preset.

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
LOCATIONS = ["LZ_HOUSTON", "LZ_NORTH"]
TZ = "America/Chicago"
DAYS = 2  # the preset's day and the next, so every replay has room to run


def segment(df: pd.DataFrame, preset: dict) -> dict:
    start_local = pd.Timestamp(preset["date_local"]).tz_localize(TZ)
    end_local = start_local + pd.Timedelta(days=DAYS)
    start_utc, end_utc = start_local.tz_convert("UTC"), end_local.tz_convert("UTC")
    part = df[(df["ts"] >= start_utc) & (df["ts"] < end_utc)]
    wide = part.pivot(index="ts", columns="location", values="price_usd_mwh").sort_index()
    local = part.drop_duplicates("ts").set_index("ts")["interval_start_local"].sort_index()
    expected = int((end_utc - start_utc) / pd.Timedelta(minutes=15))
    if len(wide) != expected or wide[LOCATIONS].isna().any().any():
        raise SystemExit(f"{preset['id']}: expected {expected} complete 15-min rows, got {len(wide)}")
    return {
        "preset": preset["id"],
        "local_dates": [(start_local + pd.Timedelta(days=d)).strftime("%Y-%m-%d") for d in range(DAYS)],
        "rows": [
            {
                "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ts_local": local[ts].isoformat(),
                **{loc: round(float(wide.at[ts, loc]), 2) for loc in LOCATIONS},
            }
            for ts in wide.index
        ],
    }


def main() -> None:
    presets = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["presets"]["items"]
    df = pd.read_parquet(SRC)
    df = df[df["location"].isin(LOCATIONS)]
    segments = [segment(df, p) for p in presets]
    header = {
        "source_note": (
            "REAL DATA. ERCOT real-time settlement point prices (MIS report 13061, 15-minute "
            "intervals), load zones LZ_HOUSTON and LZ_NORTH, for each replay preset's Chicago-local "
            "date and the day after. Exported from data/snapshot/ercot_rt_2025_hubs.parquet by "
            "web/mock/export_prices.py. Everything else the mock API serves is simulated."
        ),
        "report_type_id": 13061,
        "market": "RT",
        "units": "USD/MWh",
        "interval_minutes": 15,
        "ts_note": "ts_utc is the interval start in UTC; ts_local is the same instant in America/Chicago.",
        "locations": LOCATIONS,
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
        peaks = {loc: max(r[loc] for r in seg["rows"]) for loc in LOCATIONS}
        print(f"{seg['preset']}: {seg['local_dates']} {len(seg['rows'])} rows, peaks {peaks}")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
