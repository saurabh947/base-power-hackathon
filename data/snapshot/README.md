# Data snapshot (dev/test fixtures)

These are real ERCOT public data files, pulled 2026-09-25 by the preflight probe (`scripts/probe/build_clean.py`, `fuelmix_parse.py`).
They are committed so tests and local dev run offline. The canonical store is Postgres, loaded by `src/ingest/ercot.py`.

| file | contents |
|---|---|
| ercot_rt_2025_hubs.parquet / _2024_ | RT 15-min settlement point prices (report 13061) for HB_NORTH, HB_HOUSTON, HB_WEST, HB_SOUTH, HB_BUSAVG, LZ_HOUSTON, LZ_NORTH. LZ rows are the plain LZ, not LZEW. Columns: ts (UTC interval start), interval_start_local, location, sp_type, market, price_usd_mwh |
| ercot_da_2025_hubs.parquet / _2024_ | DA hourly settlement point prices (report 13060), same locations |
| ercot_hourly_native_load_2025.parquet | Hourly native load by weather zone plus the ERCOT total |
| ercot_fuel_mix_15min_2025.parquet | 15-min generation by fuel. Values are MW; the source file's MWh-per-15-min figures were multiplied by 4 |

**Correction (2026-09-25).** The gridstatus-built `ercot_hourly_native_load_2025.parquet` had the two passes of the 2025-11-02 fall-back hour swapped. The 06:00Z and 07:00Z rows are now in ERCOT's order, matching the M1 ingest. `tests/test_series_parity.py` pins this. Four load values still differ from Postgres in the last bit: the workbook stores 17 significant digits, and the snapshot holds them rounded.

**Addition (2026-09-26).** RT and DA files now also hold LZ_AEN (Austin) and LZ_CPS (San Antonio), from the same probe pipeline, for the Texas Triangle fleet. The other 7 locations are unchanged; this was verified identical.

**Addition (2026-09-26): 2026 year to date.** `ercot_rt_2026_hubs.parquet` and `ercot_da_2026_hubs.parquet` hold the same 9 locations in the same layout, parsed by `src.ingest.parse` from ERCOT's 2026 annual files as published on 2026-09-20 (RT `rpt.00013061...20260920.080732945.RTMLZHBSPP_2026.zip`, DA `rpt.00013060...20260920.080303414.DAMLZHBSPP_2026.zip`). They run from 2026-01-01 00:00 CST through 2026-09-19 (the last RT interval ends 2026-09-20 05:00Z), so the dev and test path can replay 2026 up to where real prices end (`src.data.years.real_until`). ERCOT republishes the current year weekly; Postgres (the cloud) may hold a later publication. `tests/test_series_parity.py` checks the two agree when they hold the same publication.
