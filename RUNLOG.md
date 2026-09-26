# RUNLOG: fleet-sim

What was built, how to run it, and open questions. The spec is `spec/FLEET_SIM_SPEC.md`.
The decisions below were agreed with the human on 2026-09-25. Where they differ from the spec, they win.

## How to run

You need uv (`~/.local/bin/uv`). uv finds a local Python 3.12 or downloads one. Run everything from the repo root.

```sh
make setup   # .venv on Python 3.12; installs numpy, pyyaml and pytest from uv.lock
make test    # every test: the seam contract suites, unit tests and the M0 acceptance test
make m0      # M0 walking skeleton; exits 1 unless Ledger.summary() reports nonzero USD
```

- **Without make:** `uv venv --python 3.12 && uv sync`, then `uv run pytest` and `uv run python -m src.sim.skeleton`.
- **Skeleton options:** `uv run python -m src.sim.skeleton --strategy peak --lag 0 --soc-init 0.9`.
  - `--strategy` overrides `strategy.name` (`bands` or `peak`).
  - `--lag` is the decision lag in ticks. 0 is spec-literal; the default comes from config (3).
  - `--soc-init` sets the starting SoC.
- **Config:** everything tunable is in `config.yaml`, which `src/config.py` loads and validates.

## Decisions: spec clarifications (accepted)

1. **Everything is measured grid-side (at the AC meter).** This covers `Command.setpoint_kw`, the fleet target and `Ledger.delivered_kwh`.
   - Discharge at P kW removes `P*dt/sqrt(eta)` from storage.
   - Charge at P kW adds `|P|*dt*sqrt(eta)` to storage.
   - `|P| <= power_kw`, and SoC stays within [0.20, 1.00].
   - A round trip returns `eta` of the energy.
2. **Locations are LZ_HOUSTON and LZ_NORTH**, with the fleet split 50/50. Hubs HB_NORTH and HB_HOUSTON are ingested for reference only.
3. **Failure detection.** A worker is DEAD after 2 missed tick deadlines OR heartbeat expiry, whichever comes first. The heartbeat stays at a 2 s SET with a 6 s TTL.
   - A worker that misses a deadline is SUSPECT. Its batteries get 0 in the next allocation, and survivors take the re-split target.
   - Batteries are re-homed round-robin. An owner epoch fences out zombie writers.
4. **The full-year backtest runs in-process and vectorized.** It uses the same physics, strategy and ledger code as live mode.
   - A parity test compares one sim day through the worker fleet with the in-process run.
   - The backtest bypasses per-command `BatteryAdapter.apply`. No frozen signature changes.
5. **No peeking.** The coordinator hands the Strategy the last *published* price tick: a lag of one 15-min interval, or 3 ticks.
   - That lagged run is the headline.
   - The spec-literal lag-0 number is stored alongside it.
6. **Strategy v1 is built exactly as specced.** The pitch shows v1, the peak rule and the perfect-hindsight ceiling.
7. **Pitch number: average $ per battery per year.**
   - Framed as gross wholesale energy value at ERCOT 2025 RT prices.
   - The fleet total and $/kW-yr and $/kWh-yr are shown below it.
   - A footnote lists what is excluded: retail and wires charges, ancillary services and degradation.
8. **Demo preset.** The replay jumps to a 2025 price-spike evening, with the fleet pre-charged (SoC 0.9).
   - It runs at 2 ticks/s.
   - Both kills happen during max discharge.
   - Killed workers stay red until reset.
9. **M1 does not wait for the human.** The agent continues once the self-check passes: 0 gaps, 0 duplicates, correct DST day counts. The human signs off the data before the pitch backtest.

## Decisions: engineering defaults

- **Prices.** 15-min RT settlement point prices (SPP), forward-filled to 5-min ticks, but only within their own 15-min interval. Gaps are never filled across intervals.
- **Other data.** DA hourly, ERCOT hourly load and 15-min fuel mix are also ingested.
- **Years.** 2025 is the headline year. 2024 is loaded for the 7-day warm-up and for comparison.
- **Ingest source.** Parse ERCOT MIS annual zips directly: reportTypeId 13061 for RT and 13060 for DA.
  - gridstatus `get_rtm_spp` duplicates LZ rows (LZ plus LZEW), so it is not used for these files.
  - Read the xlsx with calamine.
  - Prototypes are in `scripts/probe/`.
- **Command IDs.** `command_id = uuid5(NAMESPACE, f"{run_id}|{battery_id}|{tick_ts.isoformat()}")`. uuid4 per call halved throughput.
- **Idempotency.** Each battery stores `last_command_id` and `last_tick`. A command with `tick <= last_tick` is a no-op or is rejected. There is no global seen-set, which would grow to about 60 GiB/yr.
- **Strategy inputs.** The Strategy receives a symmetric `fleet_headroom_kw` (sum of `power_kw` of available batteries) and returns a value in [-H, +H].
  - The coordinator clips that value to the feasible directional headroom.
  - It then allocates in proportion to each battery's feasible headroom.
- **Per-location objects.** There is one Strategy instance per location. The Ledger gets fleet size per location in its constructor.
- **Percentile band.** Uses a 7-day window that excludes the current tick. Hold while fewer than 288 samples. Charge at or below p25; discharge at or above p90 when the price is above 0.
- **Peak strategy.** Charges 10:00-15:00 and discharges 18:00-21:00, America/Chicago, with half-open windows.
- **5% metric.** Measured against the *feasible* target. When `|target|` is near 0, an absolute floor of 0.5% of nameplate applies.
- **Redis.**
  - Streams use `MAXLEN ~`.
  - Worker processes are launched via `subprocess.Popen`.
  - Keep `XREAD BLOCK` under 5 s: redis-py's default `socket_timeout` is 5 s.
  - Cast numpy floats with `float()` before HSET.
- **Commits.** Git, with one commit per green milestone.

## Human's notes, 2026-09-25 (these override the spec)

- **Fleet: 20,000 batteries**, 20 workers x 1,000. Each battery gets random, seeded specs:
  - power 5.0-11.5 kW
  - capacity 10-20 kWh
  - power capped at capacity
  - efficiency 0.90

  The human confirmed these ranges.
- **Forecasting is added** (overrides §3 "no ML forecasting").
  - Model: Google TimesFM-3.
  - Licence: the weights are non-commercial and non-production. The human OK'd them for the demo. The pitch footnote says production would use TimesFM 2.5 (Apache-2.0), and the model is a config switch.
  - Uses: price and ERCOT-demand forecasts, a third `forecast` Strategy, and a forecast band on the site.
  - Forecasts are precomputed by a Cloud Run job and cached in Postgres, so the backtest stays fast.
- **Hosting: Google Cloud** (overrides §2 "one laptop, no cloud"). The laptop is for development only.
  - GCP project `appintellect`, region us-east1 (existing resources live there). The gcloud account is already authenticated.
  - Cloud Run service `fleet-sim` (gen2, min 0 / max 1 instance, CPU always allocated, public).
    - One container runs the API, the coordinator, 20 worker OS processes and Redis. Redis runs inside the container because sim state is meant to be ephemeral.
    - Max 1 instance means every visitor sees the same fleet.
  - Postgres on Cloud SQL. Pending: reuse the existing instance `appintellect` with a new DB and user, or create a new small instance.
  - Cloud Run jobs: `fleet-sim-ingest` (ERCOT to Postgres) and `fleet-sim-forecast` (TimesFM-3).
  - Secret Manager holds the DB password and the presenter passcode. A dedicated service account can reach only those.
- **Website on Vercel.**
  - React/Vite, talking to Cloud Run directly over CORS and WebSocket.
  - Plain language by default, with a "Show technical details" toggle.
  - Start screen: the run settings sit above the Start button.
  - Every panel explains itself.
- **Permissions.** Anyone can Start, change battery availability and reset (rate-limited). Only the presenter can kill workers, unlocked by passcode. This is the only exception to §3 "no auth".
- **Secrets.** `.env` stays on the laptop. It is gitignored, dockerignored, gcloudignored and vercelignored, and `.githooks/pre-commit` blocks .env files, Google-key-shaped strings and any value set in `.env`. Never print `.env` contents.
- **ERCOT API account.** Not needed for the build. Its credentials are parked in `.env` for a future live-price stage.

## Verified facts (preflight, 2026-09-25)

- **ERCOT 2024 and 2025 data.** RT 15-min has 35,040 rows per location in 2025 and DA hourly has 8,760, with 0 missing. No credentials are needed, and ingesting everything takes under 1 min.
- **No 5-min history without an account.** True 5-min SCED LMP history needs ERCOT API credentials. MIS keeps only about 5 days of 5-min files and about 7 days of 15-min files.
- **Speed through Redis.** 10k batteries run at about 54 ticks/s on this laptop (M3 Pro, 12 cores, 18 GB). Adopting a 500-battery cohort takes about 2 ms, and a worker cold-starts in 88 ms.
- **Speed in-process.** The vectorized path covers 10k batteries for a full year in 2-5 s.
- **Rough revenue, 2025, spec v1.** About $101-108 per 13.5 kWh / 5 kW battery per year, gross. The perfect-hindsight ceiling is about $240-260.

## Open questions (need the human)

- Go-ahead to create the GCP resources: the Cloud Run service and 2 jobs, an Artifact Registry repo `fleet-sim`, a service account `fleet-sim-run`, and 2 secrets.
- Database: reuse Cloud SQL instance `appintellect` (new DB and user, $0), or create a new small instance (~$10/month, isolated).
- The human is creating a Vercel account and will run `vercel login` themselves.
- The human will set the presenter passcode in Secret Manager using a `read -s` command, so it never appears in chat.

## M0: walking skeleton (done 2026-09-25)

**Result.** `make m0` runs 1 battery through the real LZ_HOUSTON day 2025-05-16 and passes. The loop is strategy, then allocate, then `apply`, then `read`, then the ledger. `Ledger.summary()` reports **$0.5097** for the day, which meets the nonzero-USD acceptance.
- The battery is b00000 from the seeded fleet: 10.03 kW / 14.39 kWh, eta 0.90, starting SoC 0.5. It used the bands strategy with a 3-tick lag, bought 7.58 kWh and sold 10.92 kWh.
- The other variants from `python -m src.sim.skeleton`:

  | Variant | Day's USD |
  |---|---|
  | bands, lag 0 | $0.6255 |
  | peak, lag 3 | $5.8092 |
  | peak, lag 0 | $4.4470 |

**Choice: M0 runs the bands strategy, not peak.** Its 7-day window is seeded with the 7 real days before (2025-05-09 to 2025-05-15). The skeleton simply runs whatever `config.yaml` names, with no special case. That puts the headline path (percentile window plus the 3-tick lag) on the M0 route. It also keeps M0 clear of the peak/lag question below.

**Finding: v1 sells too early on a spike day.** On 2025-05-16 the price crossed the prior week's p90 (about $50) around 13:45. The battery was empty by 15:10 and missed the evening spike ($2,250.15 at 19:45). This is how v1 behaves as specced: no forecast and no reserve. It is not a bug. That day the peak rule earned 7x more at lag 0 and 11x more at lag 3.

**What was built.**
- **Setup.** Python 3.12 venv via uv, `pyproject.toml` (requires-python >=3.12,<3.13), `uv.lock` and `.python-version`. Dependencies are numpy and pyyaml, plus pytest for dev. `src` is a package imported from the repo root, and the `Makefile` has `setup`, `test` and `m0`.
- **Config.** `config.yaml`, loaded by `src/config.py` into typed, frozen, validated dataclasses.
- **Frozen seams.** `src/core/models.py` and `src/core/interfaces.py` are spec §4 verbatim; a script checked all 39 lines. Only docstrings were added, and they pin the RUNLOG semantics.
- **Command IDs.** `src/core/ids.py`: `make_command_id` is uuid5 over run, battery and tick, with the tick normalised to UTC first. A test vector pins it.
- **Physics.** `src/sim/battery.py` is vectorized numpy: headroom in each direction, one `step` that returns new SoC plus grid-side delivered kW, metering from a SoC change, and fleet SoC. One call covers the whole 20k fleet.
- **Allocation.** `src/sim/allocation.py`: the symmetric H, a clip to the feasible directional headroom, then a split in proportion to each battery's headroom.
- **In-memory seams.**
  - `src/sim/adapters.py` holds numpy arrays plus a `last_command_id` / `last_tick` fence and counters for each battery.
  - `src/feeds/memory.py` forward-fills 15-min rows to 5-min ticks, within each interval only.
  - `src/ledger/backtest.py` takes fleet size per location in its constructor.
- **Strategies.** `src/strategy/bands.py` and `src/strategy/peak.py`. `make_strategy` swaps them via config.
- **Fleet.** `src/sim/fleet.py` draws seeded specs. Battery i gets the same specs at any fleet size.
- **M0 data.** `src/sim/m0_prices.py` holds real RT SPPs for the M0 day. Its source is documented and its sums are checksummed in tests.
- **Skeleton.** `src/sim/skeleton.py` is the M0 loop. It meters energy through `read()`, not from the command.

**Tests: 147 passing (`make test`).**
- 96 are seam contract tests, written first: battery 25, strategy 46, ledger 15, price feed 10. They are parametrized over implementation factories. To add the Postgres replay feed, the Redis adapter or a persisted ledger, add a factory to `FACTORIES` in that suite.
- 51 are unit and M0 tests: physics, allocation, config, fleet, IDs, in-memory extras and M0 acceptance.
- A mutation check injected 12 bugs one at a time, and every one was caught. Examples: the fence `<=` changed to `<`, discharge using the cell-side formula, `price > 0` changed to `>=`, day bucketing in UTC, and a closed range end.

**Spec ambiguities, and how they were resolved.**
1. **Feed contract input.** Factories take 15-min native rows, exactly as the prices table will store them, and the feed yields forward-filled 5-min ticks. So the M1 Postgres replay feed stores 15-min rows and fills in the feed.
2. **What "rejected" means when `apply()` returns None.**
   - A stale or duplicate command is a silent no-op, counted in `counters`. A replay after a crash looks the same as a late zombie, so it must not raise.
   - The adapter dedupes on `command_id` as well as the tick fence.
   - An unknown battery raises KeyError in both `apply` and `read`.
   - A naive tick or a non-finite setpoint raises ValueError, changes nothing and does not advance the fence.
   - An unavailable battery still advances its fence, so a command it consumed while offline is not replayed when it comes back.
3. **Command ID canonical form.** The tick is converted to UTC before `isoformat()`, so the same instant in any timezone gives the same ID. For UTC ticks this is identical to the RUNLOG formula.
4. **"0 when history is short"** binds strategies that use price history. Peak ignores history by definition, so the contract splits generic tests from history tests.
5. **Bands details.**
   - If p25 == p90 (a flat window), charge wins, following the spec's order.
   - A non-finite price means hold.
   - Percentiles use numpy's `linear` method.
   - The strategy trims history to its trailing 2016 ticks itself.
6. **Ledger details.**
   - `top_spike_days` is fleet revenue per America/Chicago local date (ERCOT's operating day), top 10, highest first.
   - `per_battery_avg_usd` is total / fleet size over the recorded period. Annualising is the pitch's job.
   - `record()` is additive, so workers can report cohorts separately, and it rejects non-finite values.
7. **`fleet_soc` for the Strategy** is the capacity-weighted SoC of available batteries.

**Resolved after M0 (integrator, engineering call): option (c).**
- **What changed.** The strategy's tick is now `decision_tick(now, published)` from `src/sim/decision.py`. It carries the decision-time timestamp with the latest published price, demand and fuel mix.
- **Effect.** Clock rules stay on the clock, and prices still can't be peeked at. Peak under lag 3 is now $4.4470, the same as lag 0. Bands is unchanged.
- **Where it applies.** The coordinator, the backtest and any future strategy caller must all use `decision_tick`.
- **Tests.** Pinned in `tests/test_decision_tick.py`.

## M1: data (done 2026-09-25)

- **Ingest.** `python -m src.ingest.ercot --years 2024 2025`, or `make ingest` locally and `make cloud-ingest` on Cloud SQL.
  - Loads RT and DA prices for all 15 settlement points, ERCOT native load and fuel mix.
  - Took 61 s locally from an empty DB, and a re-run is idempotent.
  - The exit code is gated by a self-check: 0 gaps, 0 duplicates, correct DST day counts.
- **Postgres backend.** `src/data/series_pg.py` returns exactly what the snapshot backend returns; prices are bit-identical.
- **Replay feed.** `src/feeds/replay.PostgresReplayFeed` streams a full year for 2 locations in 0.55 s.
- **Fixes made at integration.**
  - The snapshot's 2025 load had the 2025-11-02 fall-back hour swapped (a gridstatus artifact). It is now in ERCOT order.
  - The snapshot backend now also reads the previous ERCOT-year file.
- **Presets checked against real LZ prices.**
  - spike_evening and morning_spike are correct.
  - winter_spike was a North-only spike, so it was replaced by summer_spike (2025-07-11, both areas).
  - ordinary_day is now 2025-06-28, a median-spread day.
- **Details:** docs/notes/data.md.

## Forecasting: TimesFM-3 (done 2026-09-25)

- **Model.** `src/forecast/model.py` loads timesfm-3.0 by default; env `FORECAST_MODEL=timesfm-2.5` switches to the Apache-2.0 model. It runs on CPU torch, pinned to Hugging Face revisions.
- **Job.** `python -m src.forecast.job --year 2025` issues rolling-origin forecasts every hour:
  - RT price per location: 24 h at 15-min steps, p10/p50/p90, with DA as a known-future covariate.
  - ERCOT demand: 48 h, hourly.
- **No look-ahead.** A forecast only sees intervals that have ended. DA prices count as known from 13:30 CT the day before.
- **Accuracy** on a 2025 sample, RT MAE:
  - Houston $10.18, vs $16.10 for "same time yesterday".
  - North $10.99, vs $18.64.
  - The p10–p90 band covers only 72% of outcomes (target 80%), so the band shown on the site understates uncertainty.
- **Strategy.** `src/strategy/forecast.py` is a rank rule over the next 12 h of p50 forecasts.
  - On 152 real days: $78.00 per battery, vs bands $51.31 and peak $57.08.
  - Caveat: its parameters were chosen on those same days, so don't annualise; the full-year backtest decides.
  - On the demo day (2025-05-16) it sold at 3–4 pm and missed the evening spike.
- **Cloud.** `make cloud-forecast` builds an image with both models baked in (~2.2 GB) and runs the job. Estimated 30–60 min on 8 vCPU.
- **Details:** docs/notes/forecast.md.
