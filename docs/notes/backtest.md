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
  - Runs `python -m src.backtest.runner --pitch` in a subprocess, so replay CPU never holds the API process's GIL.
  - Runs one job at a time. While a job runs, it returns that job's id.
  - A finished job's runs become the pitch set.
- **`get_job(job_id) -> {"status", "run_ids", "error"}`**
  - Raises `KeyError` for an unknown id (404).
  - `run_ids` holds the strategy runs plus the ceiling row.
  - `error` is one plain sentence.
- **`pitch_results(year=None) -> Results`**
  - Reads the `is_pitch` rows when a database is configured, else `data/results/pitch_<year>.json`.
  - It also falls back to that file if the database fails or has no rows.
  - Raises `ResultsNotFound` (404) when neither exists.
- **`add_listener(fn)`** is called as `fn(job_id, job)` when a job ends. Use it to emit `backtest_done`.

**`backtest_runs` usage.**
- There is one row per strategy run, one ceiling row (`strategy='hindsight'`), and a skip marker for a requested strategy that could not run: totals NULL, `metrics.skipped = true`, `metrics.reason`, `metrics.reason_plain`.
- `params` holds the full typed config, the `backtest` section, battery ranges, fleet totals, the price source, the year, the label and the git commit.
- `is_pitch` means one row per (year, strategy). A pitch save flips it in one transaction.
- jsonb reorders object keys, so `params.fleet.locations` keeps the config order for display.

**Forecast.** The runner calls `make_strategy(cfg.strategy, tick_seconds, "forecast")`.
- While it is not built, the run records a skip marker, and the Results say: "The AI forecast (TimesFM-3) method isn't in these results: it isn't available in this version of the simulator yet."
- Once it exists it runs through the seam every tick, 210,240 calls per lag. It becomes the headline automatically (`backtest.headline: [forecast, bands]`).

## Open issues

- **Forecast strategy.** It is not in this build, so it is skipped and the pitch headline is currently the simple rule.
  - Keep its per-tick call well under 100 µs, for example by preloading the year's forecasts, to stay under 60 s.
- **No structured field in the Results contract** for the lag-0 variant, the 1-cycle ceiling or skip reasons. They are plain-language `assumptions` lines.
- **Ceiling scope.** It is per-day (no carry-over across midnight), and relaxed at negative prices. Both are documented above.
