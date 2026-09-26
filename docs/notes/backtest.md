# Backtest (spec M4) and the Results screen

Owner: backtest. Code in `src/backtest/`, tests in `tests/test_backtest_*.py`, targets in `mk/backtest.mk`,
settings in the `backtest:` section of `config.yaml`.

## How to run

```sh
make backtest                                   # config backtest.strategies for 2025 -> JSON (+ Postgres if configured)
make backtest BT_STRATEGIES=bands,peak BT_YEAR=2025 BT_ARGS=--no-db
make backtest-pitch                             # same, and mark the runs as the Results screen's set
make backtest-speed                             # full year x 20,000 batteries: speed + identity (1-2 min)
DATABASE_URL=postgresql://postgres@localhost:5443/fleetsim make backtest-test-pg

uv run python -m src.backtest.runner --strategies bands,peak --year 2025 [--pitch] [--no-db]
    [--no-ceiling] [--no-lag0] [--seam] [--fleet-size N] [--out DIR] [--status-file PATH]
```

- JSON goes to `data/results/` (or `$BACKTEST_RESULTS_DIR`): `runs/<run_id>.json` for every record,
  `pitch_<year>_runs.json` (the pitch set's records) and `pitch_<year>.json` (the Results object, 3.6 KB).
  The ceiling's per-kWh curves are cached in `data/results/cache/`.
- Postgres: rows go to `backtest_runs` whenever `DATABASE_URL` or `DB_HOST` is set (not with `--no-db`).
  Run `python -m src.db migrate` first. No new migration was needed.
- Prices: `src.data.series` (Postgres when configured, else the snapshot). If a database is configured
  but `src/data/series_pg.py` (M1) is not in the build, the runner reads the snapshot and records that
  in `params.prices.backend`. A caller can pass any `PriceFeed` instead (`run_backtest(..., feed=...)`).

## What each module does

| Module | Role |
|---|---|
| `engine.py` | One location's replay: seam call, headroom, `allocate`, `battery.step`, meter. Fleet split, tick loading, ledger fill, bands fast path. |
| `runner.py` | One replay per (strategy, lag) over all locations, the record, `run_backtest`, and the CLI. |
| `metrics.py` | The record's numbers (definitions below). |
| `ceiling.py` | The perfect-hindsight LP. |
| `store.py` | `backtest_runs` + JSON. |
| `results.py` | The Results object of docs/API.md. |
| `service.py` | `start_backtest`, `get_job`, `pitch_results` for the API. |

## Semantics: same as the live path

For each 5-minute tick and each location, in this order, using the shared modules:

1. `Strategy.target_kw(decision_tick(now, published), fleet_soc, H, history)`.
   - `published` is the tick `sim.decision_lag_ticks` (3) back.
   - `history` is the prices strictly before `published`, trailing `strategy.bands.window_days x ticks_per_day` (2016). This matches what the M0 skeleton passes.
   - `H` is `fleet_headroom_kw`, the symmetric headroom.
   - There is one Strategy instance per location, reused across lags because strategies are pure.
2. `headroom_kw`, then `allocate`: clip to the feasible directional headroom, then split in proportion to each battery's headroom.
3. `battery.step` on all of that location's batteries in one numpy call.
4. Grid kWh is metered from the SoC change (`grid_kwh_from_soc_change`), as the skeleton does. It is summed per (tick, location) and recorded in `BacktestLedger` at that tick's RT price. Records go in tick order, with locations in config order.

- **Fleet:** `generate_batteries(20000, seed 42)`, split into contiguous blocks in config order. LZ_HOUSTON gets b00000-b09999 and LZ_NORTH gets b10000-b19999 (`engine.build_fleets`).
- **Year:** America/Chicago operating days, 2025-01-01 00:00 to 2026-01-01 00:00 local (105,120 ticks). The bands window is warmed up with the 7 real days before (2024 data).

**Exact shortcuts.** Each one is tested to change no number.
- A hold, or a target with no feasible headroom, skips steps 2-4. `step` with zero setpoints returns the same SoC and the meter reads 0.
- `H` is computed once, because nobody takes batteries offline in a backtest.
- `fleet_soc` is recomputed only after the SoC moves.
- **Bands fast path.** Every tick's p25/p90 come from batched `np.percentile` over the same windows the seam would see.
  - The results are bit-identical: tested against the seam on 2-week windows in the default suite, and on the full year in `make backtest-speed`.
  - It is used only when `type(strategy) is BandsStrategy`.
  - `--seam` turns it off.

**Tests that pin the parity.**
- `test_engine_is_bit_identical_to_the_command_loop` compares the engine against a per-battery `Command` loop through `InMemoryBatteryAdapter`, which is what workers do. Delivered kWh, SoC after every tick and the Ledger summary are bitwise equal.
- `test_identical_to_the_m0_skeleton` checks bands and peak at lag 0 and lag 3, with and without the fast path. It gets the same summary dict, the same per-tick rows and $0.5097 / $0.6255 / $4.4470.

## Definitions (record `metrics`, Results)

- **per_battery_usd_year** = fleet total / fleet size over the replayed calendar year. `coverage.fraction` says how much of the year had prices (2025: 1.0). Nothing is annualised silently.
- **usd_per_kw_year / usd_per_kwh_year** = fleet total / sum of `power_kw`, or / sum of nameplate `capacity_kwh`.
- **cycles_per_year** = energy discharged out of storage / usable energy, where usable = capacity x (1.0 - 0.2). One cycle takes a battery from 100% down to the 20% reserve. `cycles_per_year_nameplate` divides by full capacity instead.
- **top_days** = the Ledger's `top_spike_days`: **fleet-wide USD** per America/Chicago operating day, top 10.
- **lag0** = the spec-literal lag-0 replay of the same strategy, stored in `metrics.lag0`. The headline is lag 3.
- **Also in metrics:**
  - per-location detail
  - sold/bought kWh and average sell/buy price
  - per-battery revenue percentiles
  - monthly USD
  - the storage-balance check
  - replay seconds

## Ceiling: "Best possible (perfect hindsight)"

**The LP.** One LP per operating day, solved with HiGHS via `scipy.optimize.linprog`.
- It has perfect knowledge of that day's RT prices.
- The day ends at the charge level it started at, and the LP chooses that level.
- It works at the native 15-minute settlement resolution.
- Constraints: `c + d <= power` in each interval, SoC within [0.2, 1.0], and sqrt(eta) losses each way. These are the same battery rules the strategies run under.

**Exact for all 20,000 batteries, not a sample.**
- Every constraint scales with capacity, so a battery's best revenue = capacity x F(power/capacity).
- F is solved at 16 ratios from 0.25 to 1.0.
- F is concave and nondecreasing in the ratio, so linear interpolation is a lower bound and extended neighbouring chords give an upper bound.
- In 2025 the two bounds are within 0.092% of each other ($8,122,077 to $8,129,553 for the fleet). The reported number is the interpolation, which is the lower bound.

**Two variants.**
- Uncapped: the headline "Best possible".
- At most 1 full cycle a day: a reference.

**Why the uncapped one is the headline.** The simulated batteries have no cycle limit or wear cost, and the strategies have none either (the simple rule cycles more than once on some days). So only the uncapped LP bounds what any strategy could earn under the same rules. The capped number is shown for warranty-limited operation.

**Caveats.**
- Days are planned independently. Carrying energy across midnight is not modelled, and it adds little for 1-4-hour batteries.
- At negative prices the LP may charge and discharge within one 15-minute interval. That relaxation keeps it an upper bound.
  - 2025 had 584 (Houston) and 891 (North) negative intervals, averaging about -$5/MWh.
  - The effect is well under $0.1 per battery per year.

## Measured, 2025, real ERCOT RT prices, 20,000 batteries (164.9 MW / 300.4 MWh)

| | per battery / yr | fleet / yr | Houston | Dallas-Fort Worth | $/kW-yr | $/kWh-yr | cycles/yr | lag 0 per battery |
|---|---|---|---|---|---|---|---|---|
| Simple rule (bands), lag 3 | **$119.78** | $2,395,557 | $1,183,544 | $1,212,013 | 14.53 | 7.98 | 184.5 | $117.42 |
| Evening peak, lag 3 | **$126.58** | $2,531,528 | $1,221,797 | $1,309,731 | 15.35 | 8.43 | 365.0 | $126.58 |
| Best possible (uncapped) | **$406.10** | $8,122,077 | $3,861,786 | $4,260,291 | | | 1,208 | |
| Best possible, 1 cycle/day | $323.58 | $6,471,679 | $3,039,504 | $3,432,175 | | | 365 | |

**Bands detail.**
- Sold 42,068 MWh at an average of $76.2/MWh, and bought 46,647 MWh at $17.4/MWh.
- Per-battery revenue: p10 $87.95, median $120.47, p90 $150.19. The spread comes from the heterogeneous kW/kWh.
- Top day: 2025-06-05, $69,427 for the fleet.

**Peak detail.** The top day is 2025-05-16, the demo's spike evening, at $133,666.

**Lag 0 vs lag 3.** Lag 0 earns less than lag 3 for bands. With the 15-minute lag, the rule sells slightly later into rising spikes. Peak runs on the clock, so its lag 0 and lag 3 results are equal (RUNLOG).

**2024, for comparison** (same fleet and config; there is no 2023 data to warm up with, so bands holds on Jan 1):

| | per battery / yr | lag 0 |
|---|---|---|
| bands | $113.13 | $114.72 |
| peak | $142.91 | $142.91 |
| Best possible (uncapped) | $515.51 | |
| Best possible, 1 cycle/day | $425.60 | |

**Speed** (M3 Pro), per strategy including the lag-0 replay:

| Run | Lag 3 | Lag 0 | Total |
|---|---|---|---|
| bands | 9.4 s | 7.4 s | 16.9 s |
| peak | 13.5 s | 13.3 s | 26.8 s |

- bands through the seam every tick takes 21.4 s per lag.
- The ceiling takes 10.6 s on 4 processes, and is cached after that.
- The whole pitch CLI takes 56 s of wall time.
- Price loading takes 1 s.

## API bridge (`src/backtest/service.py`)

- **`start_backtest(strategies, year) -> job_id`**
  - Validates the input and raises `ValueError` with a plain sentence (should map to 400).
  - Runs `python -m src.backtest.runner --pitch --records-out FILE` in a subprocess (its own process group), so
    replay CPU never holds the API process's GIL. The runner stores nothing; the job stores the finished records
    (see "Stopping a re-run" below).
  - Runs one job at a time. While a job runs, it returns that job's id.
  - A finished job's runs become the pitch set.
- **`get_job(job_id) -> {"status", "run_ids", "skipped", "error"}`**
  - `status` is `running`, `done`, `failed` or `cancelled`.
  - Raises `KeyError` for an unknown id (404).
  - `run_ids` holds the strategy runs plus the ceiling row (empty unless done).
  - `error` is one plain sentence.
- **`cancel_job(job_id) -> {"status": "cancelled"}`**: `KeyError` (404), `JobNotRunning` (409 `not_running`).
- **`pitch_results(year=None) -> Results`**
  - Reads the `is_pitch` rows when a database is configured, else `data/results/pitch_<year>.json`.
  - It also falls back to that file if the database fails or has no rows.
  - Raises `ResultsNotFound` (404) when neither exists.
- **`add_listener(fn)`** is called as `fn(job_id, job)` when a job ends. Use it to emit `backtest_done`.

**`backtest_runs` usage.**
- There is one row per strategy run, one ceiling row (`strategy='hindsight'`), and a skip marker for a requested strategy that could not run: totals NULL, `metrics.skipped = true`, `metrics.reason`, `metrics.reason_plain`.
- `params` holds the full typed config, the `backtest` section, battery ranges, fleet totals, the price source, the year, the label and the git commit.
- `is_pitch` means one row per (year, strategy). A pitch save flips it in one transaction.
- A skip marker never displaces a run that worked. If a public re-run's forecast fails, the Results keep the last good forecast run instead of losing the headline.
- jsonb reorders object keys, so `params.fleet.locations` keeps the config order for display.

**Ceiling curves survive a restart.** They are cached on disk in `data/results/cache/`. If that cache is missing, as it is on a fresh Cloud Run container, the curves are reused from an earlier ceiling row with the same cache key, in `backtest_runs` or the JSON pitch set. The LP grid is only re-solved when prices or physics change.

**Forecast.** The runner calls `make_strategy(cfg.strategy, tick_seconds, "forecast")`.
- While it is not built, the run records a skip marker, and the Results say: "The AI forecast (TimesFM-3) method isn't in these results: it isn't available in this version of the simulator yet."
- Once it exists it runs through the seam every tick, 210,240 calls per lag. It becomes the headline automatically (`backtest.headline: [forecast, bands]`).

## What the AI adds: `metrics.insights`, `GET /results/ai-gains`, `--daily` (2026-09-26)

### Insights stored with every record

`src/backtest/insights.py` `from_replay(replay, series)` reads the numbers off the headline (lag 3) replay that
`run_strategy` already has: each city's grid-side kWh per 5-minute tick, its fleet charge level after each tick,
and the replayed ticks' prices. **No extra replay.** The record stores them as `metrics.insights`:

| Field | Definition |
|---|---|
| `sold_mwh`, `bought_mwh` | grid-side energy sold / bought over the year |
| `avg_sell_usd_mwh`, `avg_buy_usd_mwh` | sell (buy) $ / MWh sold (bought); None when nothing moved |
| `surplus_mwh` | energy bought while the price was <= $0/MWh (surplus power soaked up) |
| `spike_mwh` | energy sold while the 5-minute price was >= $1,000/MWh |
| `spike_events` | spikes: a city's price crossing $1,000/MWh from below. **Counted per city**: the 9 pm spike of 2025-07-11 hit all four cities, so it counts 4. 2025 has 21 (docs/API.md's example), 2024 has 53. |
| `spike_charge_level` | that city's fleet charge level (capacity-weighted SoC) just before each counted spike's first step, averaged; None with no spikes. A spike at the very first replayed step has no "before" and isn't counted. |
| `spike_intervals_5min`, `spike_usd_mwh` | city-steps at or above the threshold (2025: 108), and the threshold |
| `revenue_usd` | sold x price - bought x price: the Ledger's money, recomputed (a cross-check) |

- **Same money as the pitch.** `revenue_usd` equals `fleet_total_usd` to float rounding (test: rel 1e-12). Full
  year 2025 at 20,000: bands $2,473,370.29 both ways.
- **Same numbers as RUNLOG "What the AI adds"**, reproduced from local full-year runs (snapshot prices): bands
  2025 $123.67, sold 42,332 MWh at $77.63, bought at $17.32, surplus 1,239 MWh, 27.9 MWh in spikes, 24% charged
  when they began; peak 2025 $136.70, 5,365 MWh surplus, 36.9 MWh, 35%; 2024 bands 905 MWh / 76.3 MWh / 24%,
  peak 3,552 / 298.4 / 64%. The CLI's grid context also matches (11.9 and 19.7 MW in the top 1% load hours;
  45% and 51% renewable share of charging, grid 37%).
- **Speed unchanged:** `make backtest-speed` gives bands 21.2 s and peak 32.7 s per strategy (lag 3 + lag 0 +
  insights) for the full year at 20,000 x 4 cities; from_replay itself is well under 0.1 s.
- **Not stored or served:** support during ERCOT's top 1% load hours and the wind+solar share of charging (the
  human dropped the peak-load angle). They stay in the CLI (`grid_context`) and need load and fuel mix for the year
  (Postgres, or the 2025 snapshot); without that data they print as null.
- **The engine's steps 2-4 are now one function**, `engine.deliver(target, soc, ...)` (headroom, allocate, step,
  meter). `replay_location` and the live race both call it; the existing bit-identity tests still pass.

CLI (unchanged invocation; its keys now use the API's names):

```sh
python -m src.backtest.insights --year 2025 [--strategies bands,forecast,peak] [--fleet-size N] [--json out.json]
```

### `GET /results/ai-gains` (`service.ai_gains()` -> `results.build_ai_gains`)

Built from stored runs: `store.load_gains_records()` (one slim query on backtest_runs: no curves, no config
snapshots) when a database is configured, else (or if it fails or holds none) `store.load_gains_records_json()`
over `data/results/runs/*.json` and the `pitch_<year>_runs.json` sets. Skip markers are ignored.

- **The pitch year** (config `backtest.year`) first: its `is_pitch` set, the newest flagged run per strategy. It is
  listed when it has a simple-rule run. `uplift` needs the AI run too: a build without forecasts (like a laptop)
  lists the pitch year with `bands` and `peak` only and **no `uplift`**. In the cloud both runs exist.
- **Every other year present:** its latest complete set, i.e. the newest successful `bands`, `forecast` and `peak`
  runs **on the pitch set's fleet** (same size and batteries per city; config.yaml's fleet when there is no pitch
  set). A year without all three is left out. This keeps the old two-city 2024 runs (or a small test fleet) from
  mixing into the four-city page. Other years come newest first.
- `best_possible_per_battery_usd_year` is that year's newest hindsight run on the same fleet, and `share_of_best` =
  per-battery earnings / it (3 decimals). No hindsight run: both are **left out**.
- `uplift` = AI minus simple rule: per battery, fleet, and `pct` = uplift / the simple rule's per-battery (None if
  that is <= 0).
- Labels come from config `backtest.labels`. Money is rounded to cents, MWh to 0.1, charge level and shares to
  0.001; `uplift.pct` is a fraction (0.544 = +54.4%).
- **Beyond the contract, as the website asked:** each year carries `fleet_size` (the batteries its runs simulated),
  and `spike_events` is the year's count on every strategy (spikes are defined by prices; it is taken from any run
  of the year that has insights).
- **Runs stored before `metrics.insights` existed** (everything in Cloud SQL today) are served with their money
  fields only: the eight insight fields are **left out** of their StrategyGains, and the year stays listed. Re-run
  the full-year backtests to fill them in (commands below).
- Nothing qualifies: `404 no_results`, like `/results/pitch`.

Example from local runs (2025 pitch set and 2024, 20,000 batteries, snapshot prices, into Postgres on :5446;
no forecasts locally, so no AI entry, no `uplift` and no complete 2024 set):

```json
{"ai": "forecast", "baseline": "bands", "years": [{"year": 2025, "is_pitch": true, "fleet_size": 20000,
  "best_possible_per_battery_usd_year": 421.29, "strategies": {
   "bands": {"label": "Simple rule", "per_battery_usd_year": 123.67, "fleet_usd_year": 2473370.29,
     "share_of_best": 0.294, "surplus_mwh": 1238.8, "spike_mwh": 27.9, "spike_events": 21, "spike_charge_level": 0.237,
     "sold_mwh": 42331.6, "bought_mwh": 46940.2, "avg_sell_usd_mwh": 77.63, "avg_buy_usd_mwh": 17.32},
   "peak": {"label": "Evening peak", "per_battery_usd_year": 136.7, "fleet_usd_year": 2734096.76,
     "share_of_best": 0.324, "surplus_mwh": 5365.4, "spike_mwh": 36.9, "spike_events": 21, "spike_charge_level": 0.347,
     "sold_mwh": 83202.5, "bought_mwh": 92352.2, "avg_sell_usd_mwh": 56.03, "avg_buy_usd_mwh": 20.87}}}]}
```

To fill the insight fields in Cloud SQL (after the merge is deployed, `make cloud-deploy`):

```sh
scripts/cloud/jobs.sh backtest            # the 2025 pitch set again (same numbers, now with metrics.insights)
gcloud run jobs execute fleet-sim-backtest --project appintellect --region us-east1 --wait \
  --args='^;^-m;src.backtest.runner;--strategies;bands,forecast,peak;--year;2024'
```

Tests: `tests/test_backtest_insights.py` (revenue identity, hand-checked definitions, the CLI, `--daily`),
`tests/test_backtest_ai_gains.py` (the rules; mini backtests through the JSON store, the service and the API;
the forecast runs there use synthetic forecast books, labelled as test doubles), `tests/test_backtest_store_pg.py`
(the database path gives the same object; runs without insights), `tests/test_api_unit.py` (501 bridge, 404).

### Stopping a re-run: `POST /backtest/{job_id}/cancel` (`service.cancel_job`)

- **The job now stores the results itself, not the runner.** `POST /backtest` runs
  `python -m src.backtest.runner --pitch --records-out FILE` in its own process group: the runner computes the
  records and writes them to FILE, storing nothing (`run_backtest(save=False)`). When it exits cleanly, the job
  thread stores them as the pitch set (`store.save_records(..., pitch=True)`: JSON, then backtest_runs), unless the
  job was cancelled first. That check and the "now storing" mark happen under the job lock, so a cancel either
  wins (nothing is ever stored) or comes after the point of no return.
- **Cancel:** a running job is marked `cancelled` at once (`GET /backtest/{id}` says so from then on, with no
  run_ids), its process group gets SIGTERM (the runner and the ceiling's LP pool die at once; they have no
  handler) and SIGKILL after `CANCEL_GRACE_S` (3 s) if anything is still alive. The API emits `backtest_done`
  (severity info): *"The full-year re-run was stopped. The previous results are still shown."*, or, if there were
  no results before, *"The full-year run was stopped before it finished, so there are no full-year results yet."*
  It is sent once per job (the cancel call, the job thread and a later `GET /backtest/{id}` share the event key).
- **409 `not_running`** for a job that is done, failed or already cancelled. A cancel that arrives while a
  finished job is storing its records (well under a second) waits for that to land and then answers 409: the
  results are whole and the job is done. **404 `unknown_job`** for an unknown id. A new `POST /backtest` works at
  once after a cancel (the old process may still be exiting; it can't store anything).
- Timing: the stand-in runner stops within ~0.1 s of the cancel; one that ignores SIGTERM is killed after the grace
  period; the real runner (cancelled 1.5 s in) is gone before the call returns.
- Left behind by a cancel: at most the ceiling's per-kWh curve cache in `data/results/cache/`, which is keyed by the
  prices and physics (not a result, and exactly what the next run would compute).
- The cloud job (`fleet-sim-backtest`, the CLI with `--pitch` and no `--records-out`) stores as before.
- Tests: `tests/test_backtest_cancel.py` (API: cancel while running, nothing stored, the event; SIGKILL after the
  grace period; 409 after done; 404; start again after a cancel; a cancel during the store; the real runner),
  `tests/test_backtest_store_pg.py` (no rows from a cancelled job; a finished job's rows are stored by the job),
  `tests/test_backtest_results.py` (`--records-out`).

### Requests for integrator (AI gains, cancel)

1. docs/API.md AiGains: add `fleet_size` to the year example; say which fields can be absent (the eight insight
   fields for runs stored before insights; `best_possible_per_battery_usd_year` and `share_of_best` without a
   hindsight run; the AI entry and `uplift` for a pitch year without an AI run), that `spike_charge_level` is null
   in a year without spikes, and that `spike_events` counts a spike once per city it hits.
2. Re-run the cloud backtests (commands above) so Cloud SQL's records carry `metrics.insights`; until then the page
   gets the money fields only.
3. docs/API.md cancel: the 404 code is `unknown_job` (as `GET /backtest/{id}`); the 409 message is "That full-year
   run has already finished."
4. The insights CLI's JSON keys now use the API's names (`surplus_mwh`, `spike_mwh`, `spike_events`,
   `spike_charge_level` instead of `surplus_absorbed_mwh` and `scarcity_*`); the numbers are the same.

### Picking the race's demo day: `--daily`

```sh
python -m src.backtest.insights --year 2025 --daily [--pair bands,forecast] [--top 10] [--json out.json]
```

- Replays the year for the pair (baseline, challenger; default the simple rule and the AI) and books every
  (tick, city) on its local Central-time date, for the whole day and for the evening (18:00-24:00). It prints the
  top days by challenger minus baseline for each window, with the year totals (they add up to the Ledger's).
- **Race columns.** The full-year replay carries each fleet's charge over from the day before, but the live race
  starts both fleets at the same charge. So each listed day is also replayed as the race would show it: both
  strategies from the window's start (midnight, or 6 pm) with the fleet at `battery.demo_soc_init` (90%) and a
  7-day warm-up (`race_window`, tested equal to a backtest built for that window).
- **Local run, bands vs peak, 2025, 20,000 batteries (35 s):** the whole-day list is led by 2025-05-16 (peak
  +$130,483 over the year replay; +$127,322 in a race from midnight). On the evening list the race columns show
  $0 for most days: started at 6 pm with 90% charge, the evening peak and the simple rule both sell at once and do
  the same thing. The full-year evening leads came from charge carried over from the afternoon. Pick the race's
  demo day by the race columns.
- **Forecasts live only in Cloud SQL, so the AI run is a cloud job** (after the merge is deployed; the first line
  points the backtest job at the new image):

```sh
scripts/cloud/jobs.sh backtest --no-run
gcloud run jobs execute fleet-sim-backtest --project appintellect --region us-east1 --wait \
  --args='^;^-m;src.backtest.insights;--year;2025;--daily;--pair;bands,forecast'
gcloud beta run jobs logs read fleet-sim-backtest --project appintellect --region us-east1 --freshness=30m \
  --limit=100 --order=asc
```

  Add `--year;2024` for the out-of-sample year. The job reads prices and forecasts from Cloud SQL (its `DB_*` env).

## Results follow the chosen year: 2026 across real_until_utc (2026-09-26)

docs/API.md (aee2832) "Results": `?year=`, `real_until_utc`, `real_per_battery_usd` / `projected_per_battery_usd`,
`POST /backtest {"year": 2026}`. The human chose "Results follows the chosen year". Tests:
`tests/test_backtest_years.py`, `tests/test_backtest_store_pg.py` (Postgres), the updated contract tests.

### The 2026 replay is the live replay

- `build_context(year)` reads the year's `real_until_utc` (`src.data.years`) **from the backtest's own price source**
  (`engine.price_source()`: Postgres when a database is configured, else the snapshot), so the cutoff is exactly where
  the replayed real prices end. (The cloud backtest job has `DB_*` but no `PRICE_SOURCE`; reading the cutoff through
  PRICE_SOURCE there would have taken the image's snapshot while the prices came from Cloud SQL.)
- The feed is the live coordinator's (`coordinator.run_feed`): the real feed, then `PredictedFeed` with the AI's
  stored prediction p50 (`predictions.load_all`, the latest cutoff at or before real_until), forward-filled to its three
  5-minute ticks, rounded to cents. A caller-supplied feed is treated as the real feed and gets the same prediction
  appended. A city without a prediction stops the run with a plain `NoPriceData`: "Real 2026 prices end on Sep 19, and
  the AI's prediction for the rest of the year isn't ready yet for Houston, ..., so the full year can't be replayed."
  (a full-year headline must not silently stop at the cutoff).
- The AI forecast strategy is built by `make_forecast_strategy` as the live fleet builds it: the real rolling forecasts
  issued before the cutoff, then hourly virtual forecasts sliced from the same prediction (`ForecastBook.load(...,
  virtual=True)`); the backtest passes `price_source` through `make_named_strategy` so its virtual periods start at the
  same real_until as its prices. After the cutoff its forecast is exact by construction (docs/notes/forecast.md), and the
  Results say so. The 99% coverage rule is unchanged, so the 2026 rolling forecasts must cover the real part.
- `params.real_until_utc` and `params.prices.after_real_until` ({model, cutoff_utc, issued_utc, locations, end_utc})
  record what was replayed. A fully real year (2025) is untouched: no cutoff, the same feed, the same numbers.

### The split: real and projected money

- **Strategies** (`runner.money_split`, `metrics.split`): real = the ticks before real_until_utc, each city's grid kWh
  at that tick's price; projected = the Ledger's total minus that, per city and for the fleet, so the two add up to the
  Ledger exactly. Per battery: `real_per_battery_usd` + `projected_per_battery_usd` = `per_battery_usd_year`. Plus
  `ticks_real`, `ticks_projected`, `by_location`. A real year: all real, projected 0.
- **The ceiling** splits the same way (`metrics.split`, and `split.capped` for the one-cycle variant): the per-day LP's
  days before the cutoff are real, the days from it on projected, and a day that straddles the cutoff is split
  interval by interval along its optimal schedule (not needed so far: real_until has been local midnight). The curves
  gain `uncapped_usd_real` / `capped_usd_real`; the cache key includes the cutoff only for such years, so 2025's cached
  curves and its numbers are exactly as before. Caveat: the ceiling doesn't carry charge across midnight, a strategy
  does, so a strategy can beat the ceiling's projected part alone (charge bought before the cutoff, sold after it); the
  ceiling still beats every strategy over the year.
- **Results** (`results.build_results`): `real_until_utc` (null for a real year), and on every strategy and the ceiling
  `real_per_battery_usd` and `projected_per_battery_usd`, in cents that add up to `per_battery_usd_year` to the cent
  (projected = the rounded total minus the rounded real part). Records stored before the split existed (2024, 2025)
  are real years: their split is (total, 0). For 2026 the assumptions say where real prices end and that the rest is
  the AI's prediction (a projection, shown separately), and, when the AI ran, that it trades on slices of that same
  prediction there.
- **AiGains** (`results.build_ai_gains(year=)`): the requested year first, as its own pitch set, with `is_pitch: true`;
  every other year after it with `is_pitch: false` (its own pitch set when that has all three strategies, else its
  latest complete set, on the requested set's fleet). Money is the year's total (real + projected); every
  StrategyGains and the uplift also carry `real_per_battery_usd` / `projected_per_battery_usd`, and each year
  `real_until_utc`. The slim records carry `metrics.split` (the Postgres query reads `metrics->'split'`).

### Per-year pitch sets, the API and the job

- `is_pitch` was already per (year, strategy) in `backtest_runs` and per year in the JSON export
  (`pitch_<year>_runs.json`, `pitch_<year>.json`): a 2026 pitch run replaces only 2026's rows. Tested in JSON and in
  Postgres (the 2025 flags are compared before and after).
- `GET /results/pitch?year=` and `GET /results/ai-gains?year=` pass the year to the service (default: the pitch year,
  config `backtest.year` = 2025). A malformed year is `400 invalid_year`; a year without results `404 no_results`
  ("No full-year results yet.", as the contract words it).
- `POST /backtest {"year": 2026}` runs the runner CLI for 2026 (`--pitch --records-out`) and the job stores 2026's
  pitch set; a cancel stores nothing and its `backtest_done` names the year: "The full-year 2026 run was stopped before
  it finished, so there are no full-year results for 2026 yet." (or "The full-year re-run was stopped. The previous
  results are still shown." when that year has results).
- `scripts/cloud/jobs.sh backtest` takes `YEAR` (default 2025); `make cloud-backtest YEAR=2026`.

### Example: 2026 from local data

20,000 batteries, the committed snapshot's real 2026 prices (to 2026-09-20T05:00Z), then a **SYNTHETIC** prediction
for the rest of the year (a test double: a smooth daily wave of ~$14-51/MWh per city, not TimesFM; the real one is in
Cloud SQL), `--no-db`. No TimesFM forecasts locally, so the AI is skipped (its virtual forecasts cover the predicted
28.2% of the year only). bands 25.5 s, peak 34.0 s, the ceiling 19.9 s (4 processes, 16 ratios × 1,460 city-days).

| | per battery | real (to Sep 19) | projected (Sep 20 - Dec 31) |
|---|---|---|---|
| Simple rule | $102.73 | $68.66 | $34.07 |
| Evening peak | $125.63 | $94.68 | $30.95 |
| Best possible (hindsight) | $317.49 | $277.68 | $39.81 |

The projected parts only show the mechanics: they are the synthetic wave's, not a forecast of anything. The payload
(`GET /results/pitch?year=2026`, top days cut to two):

```json
{"year": 2026, "generated_utc": "2026-09-26T22:06:41Z", "real_until_utc": "2026-09-20T05:00:00Z",
 "fleet_size": 20000, "fleet_power_mw": 164.9, "fleet_energy_mwh": 300.4,
 "assumptions": [
  "Gross wholesale energy value at real-time prices from ERCOT, the Texas grid operator (15-minute settlement), for every 5-minute step of 2026 up to Sep 19, 2026.",
  "ERCOT's real prices end on Sep 19, 2026. After that, the prices are the AI's prediction (TimesFM-3, its median), so the money after that date is a projection, shown separately.",
  "Excludes retail and delivery charges, payments for grid-support services, and battery wear.", "…",
  "The AI forecast (TimesFM-3) method isn't in these results: its AI price forecasts for Houston, Dallas–Fort Worth, San Antonio and Austin aren't ready for 2026 yet."],
 "strategies": [
  {"strategy": "bands", "label": "Simple rule", "run_id": "ee55f932-…", "is_headline": true,
   "per_battery_usd_year": 102.73, "real_per_battery_usd": 68.66, "projected_per_battery_usd": 34.07,
   "fleet_usd_year": 2054562.17, "usd_per_kwh_year": 6.84, "usd_per_kw_year": 12.46, "cycles_per_year": 234.4,
   "per_location_usd": {"LZ_HOUSTON": 694118.37, "LZ_NORTH": 857859.0, "LZ_CPS": 264368.2, "LZ_AEN": 238216.6},
   "top_days": [{"date_local": "2026-01-31", "usd": 52632.23}, {"date_local": "2026-09-02", "usd": 43890.65}]},
  {"strategy": "peak", "label": "Evening peak", "run_id": "c53dfa15-…", "is_headline": false,
   "per_battery_usd_year": 125.63, "real_per_battery_usd": 94.68, "projected_per_battery_usd": 30.95,
   "fleet_usd_year": 2512669.72, "usd_per_kwh_year": 8.37, "usd_per_kw_year": 15.24, "cycles_per_year": 365.0,
   "per_location_usd": {"LZ_HOUSTON": 706422.69, "LZ_NORTH": 1198874.08, "LZ_CPS": 320256.3, "LZ_AEN": 287116.66},
   "top_days": [{"date_local": "2026-01-25", "usd": 141248.78}, {"date_local": "2026-09-16", "usd": 91666.0}]}],
 "ceiling": {"label": "Best possible (perfect hindsight)", "per_battery_usd_year": 317.49,
             "real_per_battery_usd": 277.68, "projected_per_battery_usd": 39.81, "fleet_usd_year": 6349737.4}}
```

### In the cloud (after the merge is deployed)

2026 needs, in Cloud SQL: the 2026 prices (ingested), the 2026 rolling forecasts up to real_until (the AI's 99%
coverage rule), and the prediction from the same real_until (`price_predictions`). Then:

```sh
YEAR=2026 scripts/cloud/jobs.sh forecast          # if not done for the current real_until (resumes: new days only)
SKIP_BUILD=1 scripts/cloud/jobs.sh predict        # if no prediction for the current real_until yet (reuses the forecast image)
YEAR=2026 scripts/cloud/jobs.sh backtest          # the api image of this TAG; stores 2026's own pitch set
```

Or from the Results page, `POST /backtest {"strategies": ["bands","forecast","peak"], "year": 2026}` (the service runs
the same runner in its container).

## Open issues

- **Forecast strategy.** It is not in this build, so it is skipped and the pitch headline is currently the simple rule.
  - Keep its per-tick call well under 100 µs, for example by preloading the year's forecasts, to stay under 60 s.
- **No structured field in the Results contract** for the lag-0 variant, the 1-cycle ceiling or skip reasons. They are plain-language `assumptions` lines.
- **Ceiling scope.** It is per-day (no carry-over across midnight), and relaxed at negative prices. Both are documented above.
- **The split's projected part of the ceiling can be below a strategy's** (charge carried over the cutoff; the
  per-day LP can't): only the year's total is an upper bound. See "The split" above.
- **2026's AI number needs the 2026 rolling forecasts in Cloud SQL** up to real_until (99% coverage over the whole
  replay, real part included); without them the forecast strategy is skipped, as for any year.

### Requests for integrator (results by year)

1. docs/API.md Results: the JSON example lacks the new keys the prose names: `real_until_utc` (top level), and
   `real_per_battery_usd` / `projected_per_battery_usd` on each strategy and on the ceiling. They are always present
   (a fully real year: `real_until_utc` null, projected 0, real = the total).
2. docs/API.md AiGains: each year now carries `real_until_utc`, and every StrategyGains and the `uplift` carry
   `real_per_battery_usd` / `projected_per_battery_usd` (totals stay in `per_battery_usd_year`). `is_pitch: true`
   marks the requested year's pitch set (always the first entry, when it exists); other years are `is_pitch: false`
   and use their own pitch set when it is complete (bands, forecast, peak), else their latest complete set.
3. docs/API.md `?year=`: a malformed year is `400 invalid_year`; a year with no results is `404 no_results` with the
   same message as today ("No full-year results yet.").
4. A 2026 run without a stored prediction fails with a plain sentence (`GET /backtest/{id}` `error`): "Real 2026
   prices end on Sep 19, and the AI's prediction for the rest of the year isn't ready yet for …, so the full year
   can't be replayed." It never stores a year cut at the cutoff.
5. The cancel event of a year other than the pitch year names it: "The full-year 2026 run was stopped before it
   finished, so there are no full-year results for 2026 yet."
