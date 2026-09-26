"""Export the mock API's price fixture: REAL ERCOT prices, nothing simulated.

Reads LZ_HOUSTON and LZ_NORTH real-time 15-minute settlement point prices for
the Chicago-local dates 2025-05-16 and 2025-05-17 from the committed snapshot
(data/snapshot/ercot_rt_2025_hubs.parquet, ERCOT MIS report 13061) and writes
web/mock/data/ercot_rt_lz_2025-05-16_17.json.

Run from the repo root (after `make setup`):
    uv run python web/mock/export_prices.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data" / "snapshot" / "ercot_rt_2025_hubs.parquet"
OUT = Path(__file__).resolve().parent / "data" / "ercot_rt_lz_2025-05-16_17.json"
LOCATIONS = ["LZ_HOUSTON", "LZ_NORTH"]
START_UTC = pd.Timestamp("2025-05-16T05:00:00Z")  # 2025-05-16 00:00 CDT
END_UTC = pd.Timestamp("2025-05-18T05:00:00Z")  # 2025-05-18 00:00 CDT (exclusive)


def main() -> None:
    df = pd.read_parquet(SRC)
    df = df[df["location"].isin(LOCATIONS) & (df["ts"] >= START_UTC) & (df["ts"] < END_UTC)]
    wide = df.pivot(index="ts", columns="location", values="price_usd_mwh").sort_index()
    local = df.drop_duplicates("ts").set_index("ts")["interval_start_local"].sort_index()
    expected = int((END_UTC - START_UTC) / pd.Timedelta(minutes=15))
    if len(wide) != expected or wide[LOCATIONS].isna().any().any():
        raise SystemExit(f"expected {expected} complete 15-min rows, got {len(wide)}")

    rows = [
        {
            "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts_local": local[ts].isoformat(),
            **{loc: round(float(wide.at[ts, loc]), 2) for loc in LOCATIONS},
        }
        for ts in wide.index
    ]
    doc = {
        "source_note": (
            "REAL DATA. ERCOT real-time settlement point prices (MIS report 13061, 15-minute "
            "intervals), load zones LZ_HOUSTON and LZ_NORTH, Chicago-local dates 2025-05-16 and "
            "2025-05-17. Exported from data/snapshot/ercot_rt_2025_hubs.parquet by "
            "web/mock/export_prices.py. Everything else the mock API serves is simulated."
        ),
        "report_type_id": 13061,
        "market": "RT",
        "units": "USD/MWh",
        "interval_minutes": 15,
        "ts_note": "ts_utc is the interval start in UTC; ts_local is the same instant in America/Chicago.",
        "locations": LOCATIONS,
        "local_dates": ["2025-05-16", "2025-05-17"],
        "rows": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    peak = {loc: max(r[loc] for r in rows) for loc in LOCATIONS}
    print(f"wrote {OUT.relative_to(ROOT)}: {len(rows)} rows, peaks {peak}")


if __name__ == "__main__":
    main()
