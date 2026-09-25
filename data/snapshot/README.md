# Data snapshot (dev/test fixtures)

These are real ERCOT public data files, pulled 2026-09-25 by the preflight probe (`scripts/probe/build_clean.py`, `fuelmix_parse.py`).
They are committed so tests and local dev run offline. The canonical store is Postgres, loaded by `src/ingest/ercot.py`.

| file | contents |
|---|---|
| ercot_rt_2025_hubs.parquet / _2024_ | RT 15-min settlement point prices (report 13061) for HB_NORTH, HB_HOUSTON, HB_WEST, HB_SOUTH, HB_BUSAVG, LZ_HOUSTON, LZ_NORTH. LZ rows are the plain LZ, not LZEW. Columns: ts (UTC interval start), interval_start_local, location, sp_type, market, price_usd_mwh |
| ercot_da_2025_hubs.parquet / _2024_ | DA hourly settlement point prices (report 13060), same locations |
| ercot_hourly_native_load_2025.parquet | Hourly native load by weather zone plus the ERCOT total |
| ercot_fuel_mix_15min_2025.parquet | 15-min generation by fuel. Values are MW; the source file's MWh-per-15-min figures were multiplied by 4 |
