# Data notes: ERCOT ingest (M1) and the Postgres data layer

Owner: ingest. Written 2026-09-25. The tables between `BEGIN`/`END` markers are generated: `make ingest` writes the timings and the gap report, and `make preset-check` writes the preset check. Everything else is hand-written.

## Summary

- **M1 self-check passes for 2024 and 2025:** 0 missing intervals, 0 duplicates, and the right counts on all 4 DST days, for every series. `make ingest` loads everything from scratch in about a minute (58-64 s). A re-run takes about 51 s and writes 0 rows.
- **What is loaded:** all 15 ERCOT hubs and load zones (RT 15-min and DA hourly), plus ERCOT hourly native load and the 15-min fuel mix, for both years. That is 1.3 M price rows.
- **Matches the snapshot:** Postgres returns bit-identical RT and DA prices for the 7 snapshot locations in both years, and bit-identical 2025 fuel-mix shares.
- **One snapshot bug found:** native load 2025 matches except 2 hours where the **snapshot is wrong** (it swaps the fall-back hour's two passes), plus 4 values rounded in the last bit.
- **DST conventions verified:** ERCOT's `DST` markers in the load and fuel-mix files both mean the *repeated* (second, CST) hour, confirmed by two independent tests. The probe's fuel-mix assumption was right. The gridstatus load parse behind the snapshot was wrong.
- **`src/data/series_pg.py`** returns exactly what the snapshot backend returns (tests/test_series_parity.py).
- **`PostgresReplayFeed`** passes the PriceFeed contract suite and streams a full year for 2 locations (210,240 ticks, with demand and fuel mix) in about 0.6 s.
- **Presets:** spike_evening and morning_spike are right. winter_spike is a **North-only** spike: Houston peaks at only $215. The ordinary_day placeholder (2025-03-19) is **not** ordinary: its spread is about 2.5x the median. I recommend **2025-06-28**, or **2025-03-20** if it has to be a weekday.

## How to run

```sh
PGPORT=5441 REDIS_PORT=6441 scripts/dev/services.sh up   # any free ports; prints DATABASE_URL
PGPORT=5441 make ingest        # ERCOT 2024-2025 -> Postgres; gap report; exit 1 unless the M1 self-check passes
PGPORT=5441 make gap-report    # report + self-check over what is stored (no download)
PGPORT=5441 make preset-check  # presets vs real prices, recommended ordinary day
PGPORT=5441 make ingest-test   # data-layer tests + PriceFeed contract suite with the database
make test                      # whole suite; database tests skip without DATABASE_URL
```

- **Without make:** `DATABASE_URL=postgresql://postgres@localhost:5441/fleetsim uv run python -m src.ingest.ercot --years 2024 2025`.
- **Flags:**
  - `--datasets native_load fuel_mix rt_spp da_spp` loads a subset.
  - `--no-notes` skips writing this file.
  - `--cache-dir data/raw` is DEV ONLY: it keeps downloads and reuses them. `data/raw` is gitignored, and the real-file parser tests use it.
- **Cloud Run job:** `scripts/cloud/jobs.sh ingest` runs `python -m src.ingest.ercot --years 2024 2025 2026` (`INGEST_YEARS=2026` for the weekly current-year refresh).
  - It is stateless: it downloads into a temp dir.
  - It connects through `src.db` (`DB_HOST`/`DB_NAME`/`DB_USER`/`DB_PASSWORD`) and runs `src.db.migrate()` first.
  - The image has no `docs/` and no `data/snapshot`, so it only prints the report, and the snapshot comparison says "skipped".
  - Peak RSS is 854 MB locally, and the job has 4 GiB.
  - Postgres must be 12 or newer (`date_trunc(..., 'UTC')`).
- **Tuning:** `config.yaml` `data:` holds `years` and `max_missing_pct`.

## Sources (all public, no credentials; verified 2026-09-25)

| dataset | where | how it is found | file |
|---|---|---|---|
| RT SPP 15-min (`rt_spp`) | ERCOT MIS report 13061 "Historical RTM Load Zone and Hub Prices" | `IceDocListJsonWS?reportTypeId=13061`, then the document whose FriendlyName ends `_<year>`, latest PublishDate; download `mirDownload?doclookupId=<DocID>` | `RTMLZHBSPP_<year>.zip`: one xlsx, one sheet per month, about 14 MB |
| DA SPP hourly (`da_spp`) | MIS report 13060 "Historical DAM Load Zone and Hub Prices" | same | `DAMLZHBSPP_<year>.zip`, about 2 MB |
| Native load hourly (`native_load`) | ercot.com/gridinfo/load/load_hist | link whose file name is `Native_Load_<year>.zip` (case-insensitive) | one xlsx: `Hour Ending` plus 8 weather zones and `ERCOT` |
| Fuel mix 15-min (`fuel_mix`) | ercot.com/gridinfo/generation | a link named `IntGenbyFuel<year>*.xlsx`; the name changes yearly (2026 is `IntGenbyFuel2026-1-.xlsx`). If there is none, `FuelMixReport_PreviousYears.zip` (title "2007 - 2024"), member `IntGenbyFuel<year>.xlsx` | month sheets, MWh per 15 min |

- **Workbook reader:** every workbook is read with pandas + calamine. gridstatus is not used, and it is not a dependency.
- **2026 (the current year) is year-to-date.** ERCOT republishes the current year's files about weekly. See "2026: a year to date" below for how the parser and the self-check handle it.

## 2026: a year to date (2026-09-26)

- **The cloud ingest of 2026 failed** with `ParseError: unexpected Hour Ending value 1` in `parse_spp`. The cause was not a new layout:
  - The 2026 RT workbook (13061, published 2026-09-20) has the same 7 columns as 2025, the same point types (LZ, LZEW, HU, SH, AH), and 15-minute intervals 1-4. RTC+B (live Dec 5, 2025) didn't change the settlement interval.
  - It still has a sheet for every month. Oct, Nov and Dec are header-only. calamine reads an empty sheet as object columns, so concatenating the 12 sheets turned `Delivery Hour` (int64 in Jan-Sep) into an object column of Python ints. `_hour_ending` then took the `"HH:00"` string path and failed on `1`.
  - **Fix:** `parse_spp` leaves out header-only sheets, whole numbers are recognised in any dtype (`_whole_numbers`: ints, integral floats, digit strings), and a workbook with no price rows at all raises (the store would otherwise delete that year's rows as stale).
  - **Tests:** `tests/test_ingest_parse.py` builds a small real .xlsx in the 2026 layout (a filled Sep sheet, empty Oct-Dec), reads it through calamine like the ingest does, and checks the concatenated column really is object dtype before parsing it. With the real files cached in data/raw, the 2026 RT and DA files parse to whole days on the 15/60-minute grid for all 15 points.
- **The 2025 file is unchanged:** the RT 2025 file in data/raw (published 2026-01-01) is the one both databases hold. Its December, before and after RTC+B, has 96 intervals every day and the same point types.
- **Self-check, year to date** (`src/ingest/report.py`). A year that hasn't ended (by the ERCOT calendar, against the wall clock) is checked per series (RT, DA, native load, fuel mix) from Jan 1 up to the end of that series' last stored interval:
  - missing % counts only up to there, measured for every location up to the same end, so a location that stops early still counts as missing;
  - a DST day counts once it is published ("not yet published" otherwise, which doesn't fail);
  - the fall-back cross-check passes when that day isn't published yet;
  - the rest of the year being absent is no failure, but a gap inside the published part still is, and so is a year with nothing stored.
  - A "Year to date" section lists how far each series goes. `report.build(..., now=)` pins the clock in tests.
- **Local 2026 ingest (files published 2026-09-20):** RT and DA through Sep 19 (the last interval ends 2026-09-20 05:00Z), native load and fuel mix through Aug 31. RT rows from Sep 1 on have no `demand_mw`/`fuel_mix` context yet (349,860 of 377,220 have it); `enrich_rt` fills them in when a later ingest brings the load and fuel mix. The self-check passes.
- **Where real prices end: `real_until_utc`** (`src/data/years.py`): the END of the last RT interval present for ALL four fleet cities in the ERCOT year, from the `prices` table (or the snapshot files in the dev path). **2026-09-20T05:00:00Z** here (Sep 20 00:00 CDT: real through Sep 19); `null` for 2025. The replay switches to the AI's prediction there (docs/notes/forecast.md).
- **The 2026 snapshot** (`data/snapshot/ercot_{rt,da}_2026_hubs.parquet`, same 9 locations and layout as 2025) is the same Sep 20 publication, parsed by `src.ingest.parse`; `tests/test_series_parity.py` checks it equals Postgres when Postgres holds that publication (it does here).
- **The 2026 spike preset** was picked from the real 2026 prices: per local day, each city's highest price between 4 pm and 11 pm, ranked by the lowest of the four (so all four spike). 2026-04-24 wins: at 7:45 pm CDT Houston $1,051.54, Dallas–Fort Worth $1,231.82, San Antonio $1,096.97, Austin $1,105.20 (next: 2026-03-23, all four $939–985; the year's highest prices so far, $1,285–1,612 on 2026-01-28 around 7 am, are a morning).

## Postgres layout

| table | rows (2024+2025) | size | contents |
|---|---:|---:|---|
| `prices` (001) | 1,315,800 | 525 MB | RT: 15 points x 35,136/35,040 native 15-min rows. DA: 15 points x 8,784/8,760 hourly rows. `ts` = interval start, UTC. LZ rows are type LZ, never LZEW. |
| `native_load` (010) | 157,896 | 17 MB | `(zone, ts)`: hourly average native load, MW. Zone `ERCOT` is the system total, plus COAST, EAST, FWEST, NORTH, NCENT, SOUTH, SCENT, WEST. |
| `generation_by_fuel` (010) | 701,760 | 67 MB | `(ts, fuel)`: `gen_mw` = the file's MWh x 4, with WSL negative. `share` = fraction of positive generation, WSL excluded (NULL for WSL). `settlement_type` (all FINAL). |
| `ingest_files` (010) | 8 | small | Provenance of the last load of each (dataset, year): URL, published time, sha256, bytes, rows, download/parse/load seconds. |

Index `prices_location_market_ts (location, market, ts) INCLUDE (price_usd_mwh)` serves series_pg and the replay feed.

**Context columns on RT rows:** `demand_mw` and `fuel_mix` are filled on RT rows only; DA rows stay NULL. This is what `PriceTick.demand_mw` / `.fuel_mix` carry.
- **`demand_mw`** = ERCOT native load of the hour that contains the interval, the hourly average.
  - This is settlement data, and the hourly average includes the rest of the hour.
  - Treat it as context for display. A strategy that wants demand as a real-time signal should lag it by an hour.
- **`fuel_mix`** = `{"biomass", "coal", "gas", "gas-cc", "hydro", "nuclear", "other", "solar", "wind"}` fractions for that 15-min interval, bit-identical to `series.fuel_mix`.
- **Size cost:** the jsonb is about 272 B per row, most of the 525 MB. That is fine locally and on Cloud SQL. If size ever matters, round the shares or fill only the fleet locations.

## Idempotency

Each (dataset, year) load is one transaction:
1. COPY into a temp staging table.
2. Upsert, writing a row only when its values changed.
3. Delete rows of the same (dataset, year, location/zone/fuel) slice that the new file no longer has.

Measured results:
- **Fresh database:** everything is inserted.
- **Re-run:** `0 inserted, 0 updated, 0 stale deleted` for all 8 files, and 0 dead tuples in the data tables.
- **Stale deletion** fixes rows left at a wrong instant by an older parser (for example, a DST bug), and never touches other years, markets or locations (tests/test_ingest_store.py).
- **Precision:** floats go through COPY as their shortest exact repr, so values round-trip bit for bit. Tested with 61388.367209000004, 1/3 and 2.2e-16.

## DST handling (verified)

**RT and DA (MIS files).** Timestamps are built as follows:
- `Delivery Hour` (RT) and `Hour Ending` (DA) are hour ending 1-24. RT adds `Delivery Interval` 1-4.
- The local interval start is `date + (HE-1) h + (interval-1) x 15 min`, localized to America/Chicago.
- `Repeated Hour Flag = Y` is the second (CST) pass of the fall-back hour, and `N` is the first (CDT) pass.
- A time in the spring-forward gap raises an error, and so does a `Y` flag outside the fall-back hour.
- This reimplements gridstatus `parse_doc`, and the output equals the snapshot built with it.
- Spring-forward days have 92 RT intervals (HE03 is absent). Fall-back days have 100: HE02 appears twice, flagged N then Y.

**Native load and fuel mix: the unverified assumption, now checked.**
- Native load marks the repeated hour as `"11/02/2025 02:00 DST"`. It comes after the plain `02:00` row in the file.
- The fuel-mix November sheet adds four columns `"01:15 (DST)"`..`"02:00 (DST)"`.
- "DST" could mean either the daylight-time pass (first, CDT) or ERCOT's repeated-hour flag (second, CST).

Two independent tests settle it. `src.ingest.report.fallback_hour_evidence` re-runs both on every ingest:

1. **Load / generation ratio.** For each clock hour from 00:00 CDT to 03:00 CST, take r = ERCOT load / total generation. Load and fuel mix come from different files, so a pass misplaced in either one bends r away from the straight line through the unambiguous neighbour hours.
   - Stored order: misfit **0.0054** (2024), **0.0014** (2025).
   - Swapped order: **0.0621** and **0.0295**, 11x and 21x worse.
   - A scratch analysis of the raw files showed the same thing. Load/generation ratios agree only when both files use the same convention (spread 0.002-0.006), and are 10-14x off when they differ (0.030-0.060).
2. **Continuity.** Wind output is physically smooth. The summed 15-min jumps across the window are 5,577 MW stored vs 8,118 swapped (2024), and 2,369 vs 4,291 (2025).
   - In the scratch analysis, total generation and gas followed the same pattern.
   - The overnight load curve is also monotonic only in the stored order. For 2025 it runs 41,659, 40,309, 39,670, 39,103 MW. gridstatus's order would give 41,659, 39,670, 40,309, 39,103 MW.

**Also checked:**
- In every fuel-mix row, the interval cells including the `(DST)` ones add up to the row's `Total` column (max error 1e-6 MWh). So the `(DST)` cells belong to the fall-back day, and nothing is lost.

**Conclusion:**
- Both files use ERCOT's repeated-hour convention: `DST` = the second (CST) pass. The fuel-mix probe had assumed exactly this.
- gridstatus `get_hourly_load_post_settlements`, which built `data/snapshot/ercot_hourly_native_load_2025.parquet`, put the `02:00 DST` row first. So the snapshot's 2025-11-02 06:00Z and 07:00Z load values are swapped.
- The self-check fails the ingest if the stored order ever fits worse than the swapped one.

## Snapshot parity

Proven three ways:
1. `tests/test_ingest_parse.py::test_real_*` runs the parser on the real files, when cached in data/raw.
2. `tests/test_series_parity.py` compares `series_pg` with the snapshot backend over their whole overlap, plus DST-day, off-grid and year-boundary windows.
3. The "Snapshot parity" table in the gap report below.

| series | result |
|---|---|
| RT prices, 7 locations x 2024/2025 (491,232 rows) | bit-identical `ts`, `location`, `price_usd_mwh` and `sp_type` |
| DA prices, 7 locations x 2024/2025 (122,808 rows) | bit-identical |
| fuel mix 2025, MW (from the workbook) and shares (via series) | bit-identical, see the note below |
| native load 2025, ERCOT | identical except 2 swapped hours (above) and 4 last-bit values |

- **Why the fuel-mix shares needed care:** `series.fuel_mix` sums a row with `df.sum(axis=1)`. That sums the columns left to right on the parquet's memory layout, but uses numpy's pairwise sum on a pivoted frame, and the two differ in the last bit for 10,245 of 35,040 rows. `parse.fuel_shares` sums explicitly left to right, so it matches on any layout. There is a test for this.
- **Why 4 load values differ in the last bit:** the workbook XML literally stores values like `61388.367209000004`, and calamine keeps them. The snapshot (the openpyxl/gridstatus path) holds `61388.367209`.

## series_pg (`src/data/series_pg.py`)

- **API:** the same four functions as the snapshot backend.
  - `rt_prices` / `da_prices` return a float64 Series named after the location.
  - `system_load` returns a Series named `ERCOT`, with its index named `Interval Start`.
  - `fuel_mix` returns a DataFrame with the 9 lowercase fuel columns and `columns.name` None.
- **Index:** a `datetime64[ns, UTC]` index of interval starts, named `ts` except for `system_load`. Results are sorted and sliced to `[start, end)`.
- **Bounds:** naive bounds raise TypeError, as in the snapshot backend.
- **Empty results** equal the snapshot backend's (tested).
- **Selection:** `src.data.series` dispatches to it when `DATABASE_URL` or `DB_HOST` is set and `SERIES_BACKEND` is not `snapshot`.
- **Connections:** an optional `conn=` reuses an open connection; otherwise each call opens a short-lived one.
- **Speed for a full year:** `rt_prices` 27 ms, `da_prices` 10 ms, `system_load` 11 ms, `fuel_mix` 0.32 s.

## Replay feed (`src/feeds/replay.py`)

- **What it is:** `PostgresReplayFeed(market="RT")` implements the frozen PriceFeed Protocol.
- **Fetching:** it runs one query per 31-day chunk per location (bulk, never per tick).
- **Forward fill:** each interval's price is held for its own 5-min ticks only (3 for RT, 12 for DA). A missing interval stays missing, and an off-grid row or a non-finite price raises ValueError, the same as `forward_fill`.
- **Output:** tick timestamps are `datetime` in `timezone.utc`. `demand_mw` / `fuel_mix` come from the RT row, and the 3 ticks of an interval share one dict.
- **Speed:** measured on the real data, a full 2025 year for LZ_HOUSTON and LZ_NORTH streams in 0.55 s (105,120 ticks each). The ticks equal the snapshot's 15-min prices repeated 3x (tests/test_ingest_replay.py).
- **Test helpers:**
  - `PostgresReplayFeed.from_rows(rows)` puts rows in a TEMP table on a private connection. The contract suite's `postgres` factory uses it, so real `prices` rows are never touched.
  - `database_available()` makes the database tests skip cleanly.

## Presets (verified against real LZ_HOUSTON / LZ_NORTH RT prices)

The table below is generated. My verdicts:

- **spike_evening (2025-05-16, from 16:00 CDT): correct.**
  - LZ_HOUSTON peaks at $2,250.15 and LZ_NORTH at $2,512.18, both in the 19:45 CDT interval, 45 ticks after the start.
  - The description "above $2,000/MWh around 7:45 pm" holds for both.
- **morning_spike (2025-04-07, from 03:00 CDT): correct.**
  - LZ_HOUSTON $3,860.39 and LZ_NORTH $3,734.94, both in the 06:45-07:00 CDT interval.
  - That is the 2025 maximum for both fleet zones, so "The year's highest price, just before 7 am" holds.
- **winter_spike (2025-01-15, from 04:00 CST): North only.**
  - LZ_NORTH peaks at $2,498.61 at 07:30 CST, with 5 intervals at or above $1,000. HB_NORTH is $2,385.24, as the config comment says.
  - **LZ_HOUSTON peaks at only $215.40**, so half the fleet sees no spike.
  - Keep the day (it is the only big winter spike, and a sustained one) but fix the description. Or swap it for **2025-07-11**, where both zones spike: LZ_HOUSTON $1,754.52 and LZ_NORTH $1,757.28 in the 21:00 CDT interval, a single 15-min interval.
- **ordinary_day (placeholder 2025-03-19): not ordinary.**
  - Its daily spread is $161.86 in Houston and $157.39 in North, about 2.5x the 2025 median ($63.30 / $67.66).
  - The closest day to the median in both zones is **2025-06-28** (Sat, spreads $64.01 / $68.45, score 0.012).
  - The closest weekday is **2025-03-20** (Thu, $61.62 / $68.41, score 0.027), the day after the placeholder.

Recommended values for the integrator (who owns `presets:`):

```yaml
    - id: winter_spike
      label: "Winter morning spike"
      date_local: "2025-01-15"
      start_utc: "2025-01-15T10:00:00Z"   # 4:00 am CST; LZ_NORTH peaks $2,498.61 at 7:30 am, LZ_HOUSTON only $215.40
      description: "North Texas prices jump to about $2,500/MWh around 7:30 am; Houston stays near $200."
    - id: ordinary_day
      label: "An ordinary day"
      date_local: "2025-06-28"            # daily RT spread $64.01 (Houston) / $68.45 (North) vs 2025 medians $63.30 / $67.66
      start_utc: "2025-06-28T05:00:00Z"   # local midnight (CDT); weekday alternative: 2025-03-20, start 2025-03-20T05:00:00Z
      description: "A typical day with small ups and downs."
```

`spike_evening` and `morning_spike` need no change.

<!-- BEGIN presets -->
Generated 2026-09-26 19:50 UTC by `python -m src.ingest.presets` (series backend: postgres). RT 15-min settlement point prices; a peak's time is its interval start.

#### Presets against real prices

| preset | local date | replay starts (local) | location | peak $/MWh | peak interval (local) | ticks from start to peak | day min | day mean |
|---|---|---|---|---:|---|---:|---:|---:|
| spike_evening | 2025-05-16 | 16:00 CDT | LZ_HOUSTON | 2,250.15 | 19:45 CDT | 45 | 18.28 | 138.65 |
| spike_evening | 2025-05-16 | 16:00 CDT | LZ_NORTH | 2,512.18 | 19:45 CDT | 45 | 14.95 | 141.30 |
| spike_evening | 2025-05-16 | 16:00 CDT | LZ_CPS | 3,149.97 | 19:45 CDT | 45 | 14.66 | 165.32 |
| spike_evening | 2025-05-16 | 16:00 CDT | LZ_AEN | 2,610.95 | 19:45 CDT | 45 | 17.21 | 143.37 |
| morning_spike | 2025-04-07 | 03:00 CDT | LZ_HOUSTON | 3,860.39 | 06:45 CDT | 45 | 0.01 | 133.23 |
| morning_spike | 2025-04-07 | 03:00 CDT | LZ_NORTH | 3,734.94 | 06:45 CDT | 45 | 0.02 | 140.09 |
| morning_spike | 2025-04-07 | 03:00 CDT | LZ_CPS | 3,522.19 | 06:45 CDT | 45 | -0.47 | 120.02 |
| morning_spike | 2025-04-07 | 03:00 CDT | LZ_AEN | 3,600.84 | 06:45 CDT | 45 | -0.80 | 124.27 |
| summer_spike | 2025-07-11 | 18:00 CDT | LZ_HOUSTON | 1,754.52 | 21:00 CDT | 36 | 11.62 | 49.27 |
| summer_spike | 2025-07-11 | 18:00 CDT | LZ_NORTH | 1,757.28 | 21:00 CDT | 36 | 12.04 | 52.06 |
| summer_spike | 2025-07-11 | 18:00 CDT | LZ_CPS | 1,759.93 | 21:00 CDT | 36 | 12.44 | 49.72 |
| summer_spike | 2025-07-11 | 18:00 CDT | LZ_AEN | 1,757.40 | 21:00 CDT | 36 | 12.06 | 48.25 |
| ai_best_evening | 2025-05-30 | 18:00 CDT | LZ_HOUSTON | 388.44 | 21:00 CDT | 36 | 20.06 | 61.35 |
| ai_best_evening | 2025-05-30 | 18:00 CDT | LZ_NORTH | 389.06 | 21:00 CDT | 36 | 20.06 | 61.23 |
| ai_best_evening | 2025-05-30 | 18:00 CDT | LZ_CPS | 390.19 | 21:00 CDT | 36 | 20.06 | 61.71 |
| ai_best_evening | 2025-05-30 | 18:00 CDT | LZ_AEN | 389.25 | 21:00 CDT | 36 | 20.06 | 61.53 |
| ordinary_day | 2025-06-28 | 00:00 CDT | LZ_HOUSTON | 72.99 | 20:15 CDT | 243 | 8.98 | 28.09 |
| ordinary_day | 2025-06-28 | 00:00 CDT | LZ_NORTH | 74.93 | 20:15 CDT | 243 | 6.48 | 30.41 |
| ordinary_day | 2025-06-28 | 00:00 CDT | LZ_CPS | 77.81 | 19:45 CDT | 237 | 9.26 | 26.70 |
| ordinary_day | 2025-06-28 | 00:00 CDT | LZ_AEN | 75.09 | 19:45 CDT | 237 | 8.81 | 26.15 |
| jan1_2025 | 2025-01-01 | 00:00 CST | LZ_HOUSTON | 108.90 | 19:00 CST | 228 | 0.01 | 25.01 |
| jan1_2025 | 2025-01-01 | 00:00 CST | LZ_NORTH | 109.60 | 19:00 CST | 228 | 0.01 | 26.09 |
| jan1_2025 | 2025-01-01 | 00:00 CST | LZ_CPS | 108.07 | 19:00 CST | 228 | 0.01 | 24.03 |
| jan1_2025 | 2025-01-01 | 00:00 CST | LZ_AEN | 108.31 | 19:00 CST | 228 | 0.01 | 24.33 |
| real_prices_end_2026 | 2026-09-19 | 18:00 CDT | LZ_HOUSTON | 75.88 | 18:45 CDT | 9 | 11.88 | 29.92 |
| real_prices_end_2026 | 2026-09-19 | 18:00 CDT | LZ_NORTH | 88.86 | 18:45 CDT | 9 | 8.74 | 27.79 |
| real_prices_end_2026 | 2026-09-19 | 18:00 CDT | LZ_CPS | 86.59 | 18:30 CDT | 6 | 13.79 | 33.81 |
| real_prices_end_2026 | 2026-09-19 | 18:00 CDT | LZ_AEN | 90.02 | 18:30 CDT | 6 | 14.81 | 34.47 |
| spike_2026 | 2026-04-24 | 16:00 CDT | LZ_HOUSTON | 1,051.54 | 19:45 CDT | 45 | 19.61 | 96.78 |
| spike_2026 | 2026-04-24 | 16:00 CDT | LZ_NORTH | 1,231.82 | 19:45 CDT | 45 | 5.96 | 103.42 |
| spike_2026 | 2026-04-24 | 16:00 CDT | LZ_CPS | 1,096.97 | 19:45 CDT | 45 | 22.95 | 132.57 |
| spike_2026 | 2026-04-24 | 16:00 CDT | LZ_AEN | 1,105.20 | 19:45 CDT | 45 | 24.67 | 133.94 |
| jan1_2026 | 2026-01-01 | 00:00 CST | LZ_HOUSTON | 24.45 | 05:45 CST | 69 | 11.28 | 17.75 |
| jan1_2026 | 2026-01-01 | 00:00 CST | LZ_NORTH | 29.85 | 10:45 CST | 129 | 12.53 | 18.82 |
| jan1_2026 | 2026-01-01 | 00:00 CST | LZ_CPS | 24.40 | 05:45 CST | 69 | 10.41 | 17.17 |
| jan1_2026 | 2026-01-01 | 00:00 CST | LZ_AEN | 24.08 | 07:15 CST | 87 | 10.04 | 17.08 |

#### Ordinary day: daily RT spread (max - min) closest to the 2025 median

Median daily spread: LZ_HOUSTON $63.30/MWh, LZ_NORTH $67.66/MWh, LZ_CPS $75.34/MWh, LZ_AEN $73.69/MWh. Score = the worst relative distance from the median over the fleet locations. DST days and days with gaps are skipped.

| rank | local date | weekday | LZ_HOUSTON spread | LZ_HOUSTON max | LZ_HOUSTON mean | LZ_NORTH spread | LZ_NORTH max | LZ_NORTH mean | LZ_CPS spread | LZ_CPS max | LZ_CPS mean | LZ_AEN spread | LZ_AEN max | LZ_AEN mean | score |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 2025-09-21 | Sun | 60.46 | 62.73 | 24.92 | 64.57 | 66.84 | 25.65 | 75.40 | 77.67 | 27.91 | 72.07 | 74.34 | 28.97 | 0.046 |
| 2 | 2025-04-08 | Tue | 66.91 | 67.59 | 22.68 | 63.57 | 63.34 | 20.95 | 71.06 | 71.04 | 21.71 | 69.82 | 69.45 | 20.43 | 0.060 |
| 3 | 2025-04-01 | Tue | 61.09 | 80.91 | 36.87 | 63.55 | 77.29 | 32.83 | 70.37 | 83.30 | 35.03 | 77.56 | 91.85 | 36.01 | 0.066 |
| 4 | 2025-11-12 | Wed | 66.60 | 78.76 | 27.17 | 68.24 | 80.56 | 27.48 | 71.23 | 83.77 | 28.10 | 68.77 | 81.05 | 27.34 | 0.067 |
| 5 | 2025-07-10 | Thu | 67.69 | 78.61 | 26.82 | 69.06 | 79.92 | 28.33 | 70.15 | 81.17 | 24.10 | 69.05 | 79.97 | 26.70 | 0.069 |

Recommended ordinary_day: **2025-09-21** (replay from local midnight = `2025-09-21T05:00:00Z`).
<!-- END presets -->

## Timings

Measured on the dev laptop (M3 Pro), against local Postgres 16.

- **Fresh database, stateless (temp dir):** see the generated table below, which `make ingest` rewrites on every run. The notes were last regenerated from a fresh run into an empty database.
  - Across three fresh runs the wall time was 58-64 s. Peak RSS was 854 MB.
- **Re-run with nothing changed:** 0 rows written, 50.8 s wall.

  | year | download s | parse s | load s |
  |---|---:|---:|---:|
  | 2024 | 2.5 | 6.6 | 12.3 |
  | 2025 | 2.6 | 6.6 | 12.0 |

- **Where the time goes:**
  - Parsing is dominated by calamine reading the 22 MB RT workbook (about 4 s per year).
  - Loading is dominated by the RT upsert, which joins its jsonb context (about 11 s per year on a fresh database).
- **Download sizes per year:** RT 14 MB, DA 2 MB, load 1 MB. Fuel mix is 3 MB for 2025, but 51 MB for 2024 because it comes from the previous-years zip.
- **Wall time** also includes migrations and building the report, including the snapshot comparison.

The last run's per-file table:

<!-- BEGIN ingest-timings -->
Last `python -m src.ingest.ercot --years 2024 2025 2026` run; wall time 70.4s.

| dataset | year | MB | download s | parse s | load s | result |
|---|---|---:|---:|---:|---:|---|
| native_load | 2024 | 1.1 | 0.1 | 0.1 | 0.7 | 79056 rows: 79056 inserted, 0 updated, 0 unchanged, 0 stale deleted |
| fuel_mix | 2024 | 51.0 | 1.0 | 0.8 | 2.8 | 351360 rows: 351360 inserted, 0 updated, 0 unchanged, 0 stale deleted |
| rt_spp | 2024 | 14.3 | 1.0 | 4.3 | 7.2 | 527040 rows: 527040 inserted, 0 updated, 0 unchanged, 0 stale deleted |
| da_spp | 2024 | 2.1 | 0.3 | 0.7 | 1.6 | 131760 rows: 131760 inserted, 0 updated, 0 unchanged, 0 stale deleted |
| native_load | 2025 | 1.1 | 0.5 | 0.1 | 0.8 | 78840 rows: 78840 inserted, 0 updated, 0 unchanged, 0 stale deleted |
| fuel_mix | 2025 | 3.0 | 0.3 | 0.8 | 2.8 | 350400 rows: 350400 inserted, 0 updated, 0 unchanged, 0 stale deleted |
| rt_spp | 2025 | 14.0 | 0.0 | 4.3 | 9.8 | 525600 rows: 525600 inserted, 0 updated, 0 unchanged, 0 stale deleted (cached file) |
| da_spp | 2025 | 2.1 | 0.0 | 0.7 | 2.1 | 131400 rows: 131400 inserted, 0 updated, 0 unchanged, 0 stale deleted (cached file) |
| native_load | 2026 | 0.7 | 0.1 | 0.1 | 0.6 | 52479 rows: 52479 inserted, 0 updated, 0 unchanged, 0 stale deleted |
| fuel_mix | 2026 | 2.0 | 0.1 | 0.5 | 2.4 | 233240 rows: 233240 inserted, 0 updated, 0 unchanged, 0 stale deleted |
| rt_spp | 2026 | 9.8 | 0.0 | 3.1 | 7.4 | 377220 rows: 377220 inserted, 0 updated, 0 unchanged, 0 stale deleted (cached file) |
| da_spp | 2026 | 1.5 | 0.0 | 0.5 | 1.5 | 94305 rows: 94305 inserted, 0 updated, 0 unchanged, 0 stale deleted (cached file) |

| year | download s | parse s | load s | total s |
|---|---:|---:|---:|---:|
| 2024 | 2.4 | 6.0 | 12.3 | 20.7 |
| 2025 | 0.7 | 5.9 | 15.5 | 22.1 |
| 2026 | 0.2 | 4.1 | 11.9 | 16.2 |
<!-- END ingest-timings -->

## Gap report

<!-- BEGIN gap-report -->
Generated 2026-09-26 18:54 UTC by `python -m src.ingest.report`.

### Year to date

2026: not over yet, and ERCOT republishes the current year's files weekly. Each series is checked from Jan 1 up to the end of its last stored interval; the rest of the year is not counted as missing, and a DST day counts once it is published.

| year | series | checked up to (end of the last stored interval, UTC) | local |
|---|---|---|---|
| 2026 | RT | 2026-09-20 05:00Z | 2026-09-20 00:00 CDT |
| 2026 | DA | 2026-09-20 05:00Z | 2026-09-20 00:00 CDT |
| 2026 | native_load | 2026-09-01 05:00Z | 2026-09-01 00:00 CDT |
| 2026 | fuel_mix | 2026-09-01 05:00Z | 2026-09-01 00:00 CDT |

### Prices: rows, gaps and duplicates per (location, market, year)

Expected = intervals in the ERCOT (America/Chicago) calendar year (a year to date: up to its last stored interval): 15-min for RT, hourly for DA. Duplicates = rows sharing (ts, location, market). Prices in $/MWh.

| location | market | year | rows | expected | missing | missing % | duplicates | off-grid | min | max | mean |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| HB_BUSAVG | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -31.98 | 4,981.33 | 26.47 |
| HB_HOUSTON | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -32.00 | 4,981.41 | 26.89 |
| HB_HUBAVG | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -32.00 | 4,981.33 | 26.83 |
| HB_NORTH | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -65.55 | 4,981.35 | 25.89 |
| HB_PAN | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -37.64 | 4,981.33 | 19.67 |
| HB_SOUTH | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -70.24 | 4,981.23 | 27.07 |
| HB_WEST | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -31.90 | 4,981.33 | 27.45 |
| LZ_AEN | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -31.93 | 4,981.30 | 29.31 |
| LZ_CPS | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -228.75 | 4,981.23 | 28.94 |
| LZ_HOUSTON | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -32.00 | 4,981.42 | 27.20 |
| LZ_LCRA | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -50.98 | 4,981.30 | 29.72 |
| LZ_NORTH | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -31.97 | 4,981.35 | 26.41 |
| LZ_RAYBN | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -31.99 | 4,981.35 | 26.23 |
| LZ_SOUTH | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -32.23 | 4,981.13 | 30.43 |
| LZ_WEST | RT | 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | -31.87 | 4,981.32 | 35.71 |
| HB_BUSAVG | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -2.28 | 2,221.46 | 27.68 |
| HB_HOUSTON | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -0.02 | 2,215.08 | 28.27 |
| HB_HUBAVG | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -2.85 | 2,221.48 | 28.09 |
| HB_NORTH | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -4.00 | 2,224.74 | 27.06 |
| HB_PAN | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -33.94 | 2,218.42 | 21.16 |
| HB_SOUTH | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -2.82 | 2,209.61 | 28.10 |
| HB_WEST | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -13.30 | 2,236.49 | 28.94 |
| LZ_AEN | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | 0.49 | 2,274.50 | 30.88 |
| LZ_CPS | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | 0.53 | 2,285.26 | 31.02 |
| LZ_HOUSTON | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -0.02 | 2,215.15 | 28.47 |
| LZ_LCRA | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | 0.33 | 2,347.76 | 31.70 |
| LZ_NORTH | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -3.83 | 2,227.02 | 27.57 |
| LZ_RAYBN | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -4.19 | 2,224.11 | 27.45 |
| LZ_SOUTH | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -4.53 | 2,196.85 | 30.23 |
| LZ_WEST | DA | 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | -9.76 | 2,256.49 | 36.92 |
| HB_BUSAVG | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -32.05 | 3,578.65 | 32.27 |
| HB_HOUSTON | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -188.54 | 3,777.15 | 33.05 |
| HB_HUBAVG | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -31.88 | 3,553.10 | 32.49 |
| HB_NORTH | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -32.95 | 3,740.11 | 32.02 |
| HB_PAN | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -34.82 | 3,708.86 | 23.82 |
| HB_SOUTH | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -86.84 | 2,995.76 | 32.02 |
| HB_WEST | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -31.20 | 3,699.40 | 32.88 |
| LZ_AEN | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -32.53 | 3,600.84 | 35.93 |
| LZ_CPS | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -32.11 | 3,522.19 | 35.54 |
| LZ_HOUSTON | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -36.51 | 3,860.39 | 33.19 |
| LZ_LCRA | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -32.19 | 3,601.46 | 37.46 |
| LZ_NORTH | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -32.95 | 3,734.94 | 32.66 |
| LZ_RAYBN | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -32.93 | 3,742.22 | 32.71 |
| LZ_SOUTH | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -32.73 | 3,001.73 | 33.50 |
| LZ_WEST | RT | 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | -30.32 | 3,686.94 | 42.18 |
| HB_BUSAVG | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -0.87 | 836.83 | 33.24 |
| HB_HOUSTON | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -0.10 | 772.08 | 34.47 |
| HB_HUBAVG | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -2.02 | 813.12 | 33.50 |
| HB_NORTH | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | 0.00 | 913.72 | 32.94 |
| HB_PAN | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -26.83 | 915.26 | 25.44 |
| HB_SOUTH | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -1.81 | 658.80 | 32.84 |
| HB_WEST | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -13.21 | 911.12 | 33.74 |
| LZ_AEN | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -0.56 | 793.50 | 36.94 |
| LZ_CPS | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -0.06 | 845.75 | 37.09 |
| LZ_HOUSTON | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -0.10 | 788.76 | 34.57 |
| LZ_LCRA | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -0.53 | 1,322.05 | 38.17 |
| LZ_NORTH | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -0.03 | 959.39 | 33.40 |
| LZ_RAYBN | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -0.02 | 940.61 | 33.68 |
| LZ_SOUTH | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -2.96 | 746.67 | 33.33 |
| LZ_WEST | DA | 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | -9.00 | 1,037.97 | 42.72 |
| HB_BUSAVG | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -31.49 | 1,308.54 | 31.74 |
| HB_HOUSTON | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -251.00 | 1,170.38 | 32.75 |
| HB_HUBAVG | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -52.90 | 1,281.45 | 31.32 |
| HB_NORTH | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -42.64 | 1,596.92 | 32.37 |
| HB_PAN | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -34.00 | 1,573.10 | 25.11 |
| HB_SOUTH | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -31.30 | 1,297.17 | 31.09 |
| HB_WEST | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -31.62 | 1,522.50 | 29.05 |
| LZ_AEN | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -32.16 | 1,311.74 | 33.74 |
| LZ_CPS | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -32.01 | 1,346.93 | 33.36 |
| LZ_HOUSTON | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -23.51 | 1,284.81 | 33.42 |
| LZ_LCRA | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -31.82 | 1,357.68 | 34.01 |
| LZ_NORTH | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -34.44 | 1,611.92 | 33.90 |
| LZ_RAYBN | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -34.51 | 1,719.56 | 34.11 |
| LZ_SOUTH | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -31.34 | 1,284.88 | 31.98 |
| LZ_WEST | RT | 2026 | 25,148 | 25,148 | 0 | 0.000% | 0 | 0 | -30.02 | 1,485.18 | 32.45 |
| HB_BUSAVG | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -3.32 | 1,831.71 | 34.38 |
| HB_HOUSTON | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -0.13 | 1,704.92 | 35.57 |
| HB_HUBAVG | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -4.17 | 1,817.08 | 34.08 |
| HB_NORTH | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -6.00 | 1,899.19 | 34.84 |
| HB_PAN | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -28.27 | 1,973.89 | 28.18 |
| HB_SOUTH | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -3.55 | 1,664.02 | 33.75 |
| HB_WEST | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -19.40 | 2,000.17 | 32.15 |
| LZ_AEN | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -4.00 | 2,160.97 | 38.41 |
| LZ_CPS | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -3.30 | 2,279.59 | 38.51 |
| LZ_HOUSTON | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -0.07 | 1,694.30 | 35.89 |
| LZ_LCRA | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -3.56 | 2,002.82 | 37.84 |
| LZ_NORTH | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -6.09 | 1,937.00 | 35.92 |
| LZ_RAYBN | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -5.62 | 1,921.71 | 36.27 |
| LZ_SOUTH | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -5.66 | 1,537.74 | 33.40 |
| LZ_WEST | DA | 2026 | 6,287 | 6,287 | 0 | 0.000% | 0 | 0 | -19.69 | 2,032.11 | 35.37 |

### DST days: intervals per local (America/Chicago) day

Spring-forward days have 23 hours (92 x 15-min), fall-back days 25 (100 x 15-min).

| series | 2024-03-10 (spring) | 2024-11-03 (fall) | 2025-03-09 (spring) | 2025-11-02 (fall) | 2026-03-08 (spring) | 2026-11-01 (fall) |
|---|---:|---:|---:|---:|---:|---:|
| RT prices (each of 15 locations) | 92 (want 92) | 100 (want 100) | 92 (want 92) | 100 (want 100) | 92 (want 92) | not yet published |
| DA prices (each of 15 locations) | 23 (want 23) | 25 (want 25) | 23 (want 23) | 25 (want 25) | 23 (want 23) | not yet published |
| native load (ERCOT, hourly) | 23 (want 23) | 25 (want 25) | 23 (want 23) | 25 (want 25) | 23 (want 23) | not yet published |
| fuel mix (15-min) | 92 (want 92) | 100 (want 100) | 92 (want 92) | 100 (want 100) | 92 (want 92) | not yet published |

### Native load (ERCOT total, hourly)

| year | hours | expected | missing | missing % | duplicates | off-grid | zones | min MW | max MW | mean MW |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2024 | 8,784 | 8,784 | 0 | 0.000% | 0 | 0 | 9 | 35,856 | 85,199 | 52,538 |
| 2025 | 8,760 | 8,760 | 0 | 0.000% | 0 | 0 | 9 | 38,363 | 83,679 | 55,734 |
| 2026 | 5,831 | 5,831 | 0 | 0.000% | 0 | 0 | 9 | 39,680 | 91,134 | 58,978 |

### Fuel mix (15-min generation by fuel)

Shares are fractions of positive generation with WSL excluded; they must sum to 1.

| year | intervals | expected | missing | missing % | duplicates | off-grid | fuels | max abs(sum(shares) - 1) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2024 | 35,136 | 35,136 | 0 | 0.000% | 0 | 0 | 10 | 4.4e-16 |
| 2025 | 35,040 | 35,040 | 0 | 0.000% | 0 | 0 | 10 | 4.4e-16 |
| 2026 | 23,324 | 23,324 | 0 | 0.000% | 0 | 0 | 10 | 4.4e-16 |

### Context on RT rows (what the replay feed puts in PriceTick.demand_mw / fuel_mix)

| year | RT rows | with demand_mw | with fuel_mix |
|---|---:|---:|---:|
| 2024 | 527,040 | 527,040 | 527,040 |
| 2025 | 525,600 | 525,600 | 525,600 |
| 2026 | 377,220 | 349,860 | 349,860 |

### Fall-back hour placement (DST cross-check)

Native load flags the repeated hour as `HH:00 DST`, the fuel mix as `(DST)` columns. Both are stored as the second, CST pass (the MIS Repeated Hour Flag convention). The stored order must fit better than the two passes swapped on both tests.

| fall-back day | load/gen ratio misfit: stored | swapped | wind jumps MW: stored | swapped | verdict |
|---|---:|---:|---:|---:|---|
| 2024-11-03 | 0.0054 | 0.0621 | 5,577 | 8,118 | stored order fits better |
| 2025-11-02 | 0.0014 | 0.0295 | 2,369 | 4,291 | stored order fits better |
| 2026-11-01 | n/a | n/a | n/a | n/a | not yet published |

### Snapshot parity (Postgres vs data/snapshot, same series API)

| series | year | rows compared | result |
|---|---|---:|---|
| RT prices, 9 locations | 2024 | 316,224 | identical |
| DA prices, 9 locations | 2024 | 79,056 | identical |
| RT prices, 9 locations | 2025 | 315,360 | identical |
| DA prices, 9 locations | 2025 | 78,840 | identical |
| native load, ERCOT | 2025 | 8,760 | 4 values differ in the last bit (snapshot rounded) |
| fuel mix shares | 2025 | 315,360 | identical |

### Source files and timings (last load of each file)

| dataset | year | file | published | MB | rows loaded | download s | parse s | load s | ingested (UTC) |
|---|---|---|---|---:|---:|---:|---:|---:|---|
| da_spp | 2024 | rpt.00013060.0000000000000000.20250101.080733817.DAMLZHBSPP_2024.zip | 2025-01-01 | 2.1 | 131,760 | 0.3 | 0.7 | 1.6 | 2026-09-26 18:53 |
| fuel_mix | 2024 | FuelMixReport_PreviousYears.zip [^intgenbyfuel2024\.xlsx?$] | 2025-03-07 | 51.0 | 351,360 | 1.0 | 0.8 | 2.8 | 2026-09-26 18:53 |
| native_load | 2024 | Native_Load_2024.zip | 2025-01-10 | 1.1 | 79,056 | 0.1 | 0.1 | 0.7 | 2026-09-26 18:53 |
| rt_spp | 2024 | rpt.00013061.0000000000000000.20250101.081829672.RTMLZHBSPP_2024.zip | 2025-01-01 | 14.3 | 527,040 | 1.0 | 4.3 | 7.2 | 2026-09-26 18:53 |
| da_spp | 2025 | rpt.00013060.0000000000000000.20260101.084409719.DAMLZHBSPP_2025.zip | 2026-01-01 | 2.1 | 131,400 | 0.0 | 0.7 | 2.1 | 2026-09-26 18:54 |
| fuel_mix | 2025 | IntGenbyFuel2025.xlsx | 2026-03-09 | 3.0 | 350,400 | 0.3 | 0.8 | 2.8 | 2026-09-26 18:53 |
| native_load | 2025 | Native_Load_2025.zip | 2026-02-04 | 1.1 | 78,840 | 0.5 | 0.1 | 0.8 | 2026-09-26 18:53 |
| rt_spp | 2025 | rpt.00013061.0000000000000000.20260101.132601474.RTMLZHBSPP_2025.zip | 2026-01-01 | 14.0 | 525,600 | 0.0 | 4.3 | 9.8 | 2026-09-26 18:54 |
| da_spp | 2026 | rpt.00013060.0000000000000000.20260920.080303414.DAMLZHBSPP_2026.zip | 2026-09-20 | 1.5 | 94,305 | 0.0 | 0.5 | 1.5 | 2026-09-26 18:54 |
| fuel_mix | 2026 | IntGenbyFuel2026-1-.xlsx | 2026-09-08 | 2.0 | 233,240 | 0.1 | 0.5 | 2.4 | 2026-09-26 18:54 |
| native_load | 2026 | Native_Load_2026.zip | 2026-09-11 | 0.7 | 52,479 | 0.1 | 0.1 | 0.6 | 2026-09-26 18:54 |
| rt_spp | 2026 | rpt.00013061.0000000000000000.20260920.080732945.RTMLZHBSPP_2026.zip | 2026-09-20 | 9.8 | 377,220 | 0.0 | 3.1 | 7.4 | 2026-09-26 18:54 |

### Self-check

- **PASS** every file downloaded, parsed and loaded
- **PASS** prices: missing < 0.1% for every (location, market, year): worst 0.000%
- **PASS** prices: 0 duplicates and 0 off-grid timestamps: 0 duplicates, 0 off-grid
- **PASS** DST days have the right interval counts
- **PASS** native load: missing < 0.1%, 0 duplicates: 0 duplicates/off-grid
- **PASS** fuel mix: missing < 0.1%, 0 duplicates, shares sum to 1: 0 duplicates/off-grid
- **PASS** fall-back repeated hour placed consistently in load and fuel mix: 2 fall-back day(s) checked, 1 not yet published
- **PASS** Postgres matches data/snapshot where they overlap (known differences excepted)

**Result: PASS**
<!-- END gap-report -->

## Open issues and risks

1. **The snapshot's native load 2025 is wrong on the fall-back day:** 06:00Z and 07:00Z are swapped. The parity test allows exactly that difference. Fix: regenerate the file from Postgres (request below).
2. **`src/data/series.py` snapshot backend, new-year gap:** `_prices` chooses files by the UTC year of the bounds. A query inside `[2026-01-01 00:00Z, 06:00Z)` returns nothing, although those hours belong to ERCOT 2025 and are in the 2025 file. Postgres returns them (tested). Fix: also try `start.year - 1` (request below).
3. **`demand_mw` semantics:** it is the hourly average post-settlement native load, so it includes the rest of the current hour. Use it for display. A strategy should lag it.
4. **`prices` is 525 MB,** mostly the per-row jsonb fuel mix. This is acceptable for now. See "Context columns on RT rows" above for how to shrink it.
5. **Scraping risk:** discovery reads the load_hist and generation pages. If ERCOT renames links, discovery fails loudly (SourceError) and the self-check fails. It never loads partial data silently.
6. **Current-year files** (2026) are year-to-date. The fuel-mix workbook may hold non-FINAL settlement types, which are stored in `settlement_type`.
7. **`DATABASE_URL` switches `src.data.series` to Postgres for every caller.** That includes other areas' tests if they use `series` while `DATABASE_URL` is set. The data is identical except the snapshot's DST bug.

## Requests for integrator

- **Regenerate `data/snapshot/ercot_hourly_native_load_2025.parquet` from Postgres.** It is outside my lane. After that, `describe_load_diff` reports "identical" and the parity test's swap assertion must flip. Until then, the documented exception stays.
- **Fix the new-year gap in `src/data/series.py::_prices`** (issue 2).
- **Update `presets:` in config.yaml** with the values recommended above.
- **Add a note to `data/snapshot/README.md`** about the load snapshot's DST swap.
