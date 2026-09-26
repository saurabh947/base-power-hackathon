# Fleet (M2, M3) and backend API: notes

Owner: fleet + API builder. Spec §7, RUNLOG decisions 3–5 and the engineering defaults.

## How to run

```sh
make setup
make fleet-up            # Postgres :5442 + Redis :6442 (project-local)
make fleet-test          # fleet, recovery, idempotency, API, adapter suites against that Redis (~1 min)
make fleet-test-all      # the whole suite with Redis reachable (nothing redis-marked skipped)
make fleet-m2            # M2: 1 sim day, 20,000 batteries, 20 worker processes, max speed
make fleet-m3            # M3: SIGKILL worker 7 during max discharge at max speed
make fleet-m3-2tps       # M3 at the demo speed, 2 ticks/s
make fleet-parity        # one sim day through the fleet vs src.backtest: bit-identical (SoC, kWh, ledger)
make fleet-serve         # whole backend on :8442 (coordinator + 20 workers + API)
make fleet-down
```

- Plain `make test` runs everything; tests marked `redis` skip cleanly when `REDIS_URL` (default `redis://127.0.0.1:6379/0`) is unreachable.
- Tests use unique key prefixes and delete only their own keys, so they are safe on a shared Redis.
- Container: `python -m src.launcher` with `START_REDIS=1` (the Dockerfile already sets it).

## Processes

```
launcher ── redis-server (START_REDIS=1)
         ├─ coordinator ── 20 × `python -m src.sim.worker` (subprocess.Popen, SIGKILL-able)
         └─ uvicorn src.api.main:app
```

- The launcher exits non-zero if any of Redis, the coordinator or the API dies. SIGTERM stops everything with exit 0 in about 1 s.
- Children poll `os.getppid()` (macOS has no PDEATHSIG):
  - workers watch the coordinator;
  - the coordinator and the API watch the launcher, via `FLEET_PARENT_PID`.
- The coordinator reaps SIGKILLed workers with `Popen.poll()`. In the container, tini reaps orphans.

## Redis layout

Every key sits under an optional prefix (`FLEET_REDIS_PREFIX`, `""` in production). Every stream is trimmed with `MAXLEN ~`.

| Key | What |
|---|---|
| `battery:{id}` | Hash per battery (`b00000`…`b19999`), the source of truth. Fields: `soc, last_command_id, last_tick, owner_epoch, available, owner, run_id, battery_id, location, idx, capacity_kwh, power_kw, efficiency`. The dynamic fields come first, because listpack hashes are scanned linearly. |
| `commands:{w}` | One msgpack batch per worker per tick: global indices (int32), setpoints (f64), availability (u8), plus an optional `load` section of adopted batteries. Also carries `load` (run start) and `stop` messages. |
| `telemetry` | Per tick, per worker: indices, post-tick SoC (f64) and delivered kW (f64) per battery, plus counts. `ready` acks go here too. |
| `hb:{w}` | `SET … PX 6000` every 2 s, while the worker's lease is still valid. |
| `lease:{w}` | The worker's epoch. On DEAD the coordinator overwrites it; this is the fence. |
| `dead_letter`, `dead_letter:count` | One entry per failed command (JSON with reason), plus a per-run counter. |
| `control`, `reply:{op}` | API → coordinator requests and their replies. |
| `snapshot`, `events`, `ticks`, `workers`, `layout`, `kw`, `epoch`, `coordinator` | Read by the API. `workers` is the pid registry used by `/chaos/kill`. `kw` is float32 current power per battery. |

## Design decisions

### Write-through state, fenced by a lease

- Workers keep cohort state in numpy arrays.
- Each tick, one `WATCH lease:{w}` → `GET` → `MULTI` / N×`HSET soc last_command_id last_tick` / `XADD telemetry` / `EXEC` transaction does two things at once:
  1. writes the idempotency fence in the same write as `soc`;
  2. makes state and telemetry atomic.
- The hot path is built as raw RESP. That measured +30% throughput over redis-py pipelines.
- No Lua. Lua check-and-apply halved throughput in the preflight.
- A zombie (a frozen worker that wakes after being declared DEAD) finds its lease changed, so its `EXEC` aborts. It dead-letters its batch as `epoch_mismatch` and exits with code 3. Tested at worker scale and at fleet scale.

### Idempotency is per battery

- Every command is deduped on `last_command_id` AND the tick fence (`tick <= last_tick` is a no-op). There is no global seen-set.
- Command ids use `make_command_id`'s exact formula, vectorized in `CommandIds` (sha1 prefix per battery, numpy formatting).
  - This is about 6× faster: 0.38 ms per 1,000 ids.
  - A test pins bit-identical output.
- Duplicates are counted but not dead-lettered. They weren't failures; they had already been applied.

### Deadline barrier

- The formula is `min(cap 250 ms, max(floor 100 ms, 50% of the tick period))`:
  - 1–2 ticks/s → 250 ms;
  - 5–10 ticks/s → 100 ms;
  - `max` → 250 ms.
- The 100 ms floor is my addition. On the M3 Pro the barrier p50 at 20k is about 26 ms, but on 4 Cloud Run vCPUs a 50 ms deadline at 10 ticks/s risks false SUSPECT/DEAD. Set `orchestration.deadline_floor_ms: 0` for the spec-literal value.

### SUSPECT and DEAD

- A worker that misses the deadline is SUSPECT. Its batteries are excluded from the strategy's H and get setpoint 0; the target is re-split over the survivors.
- It still receives its zero batch, so it can prove liveness. An on-time report makes it healthy again.
- It is DEAD after 2 consecutive misses or when its heartbeat key expires, whichever comes first.

### Recovery

On DEAD, `_declare_dead` does the following, in order:

1. Fence: overwrite `lease:{w}`.
2. Drain telemetry, so anything committed before the fence is booked.
3. Reassign the cohort round-robin over healthy workers, with `owner` / `owner_epoch` written as raw RESP (about 2 ms for 1,000).
4. Dead-letter the never-confirmed commands as `expired_owner_dead`.

Adopters HMGET the batteries lazily, from the `load` section of their very next batch, so the cohort is reassigned with 0 ticks in between.

### Fleet split and battery ids

- Battery g is `b{g:05d}` from `src.sim.fleet.generate_batteries`. `src.sim.fleet.assign_locations` gives each location a contiguous block in config order, the same split the backtest uses: at 20,000 / 50-50, LZ_HOUSTON is b00000–b09999 and LZ_NORTH is b10000–b19999.
- Worker w starts with chunk w of every location (500 + 500 at 20k/20).
- People see "Controller N" with N = worker_id + 1.

### Ledger and metering (parity with the backtest)

- Grid-side kWh is metered from each battery's SoC change (`grid_kwh_from_soc_change`, as the skeleton and backtest do). It is summed over the location's whole array in index order and recorded per tick, locations in config order.
- A missing cohort moved 0.
- `actual_kw` = metered kWh / dt.
- Late telemetry from a SUSPECT worker that did apply its commands is booked additively at its own tick's price, because that energy really moved. So running totals can exceed the sum of per-tick `revenue_usd` by that amount.

### Strategy inputs (parity with the skeleton and backtest)

- The warm-up starts exactly `window_days` (7) before the replay start.
- `published` is the tick `decision_lag_ticks` positions earlier in that location's tick list.
- `price_history` is the ≤ 2016 prices before it. In the first 3 ticks it is clamped by the warm-up start, exactly as `src/sim/skeleton.py` and `src.backtest.engine.replay_location` clamp it.

### Run start

1. The coordinator sends each live worker a `load` message.
2. The worker initialises its batteries (`soc0`, empty fence, `run_id`) in its own fenced transaction. Doing it in the worker, ordered after any in-flight old-run batches in its stream, prevents a lagging worker from clobbering the new run.
3. The coordinator waits for every `ready`, then ticks.

### Strategies

- One instance per location: `make_strategy(cfg.strategy, tick_seconds, name)` with no extra context.
- `forecast` falls back to bands, and `run_started` is sent with severity `warn`, in two cases:
  - it can't be built;
  - its cached forecasts cover less than 90% of the first replayed day (issued at or before, not older than `max_age_hours`, as the backtest measures coverage).
- Serve mode warms the forecast strategies in a background thread every ~8 min, so a Start never waits for a year of forecasts to load from Cloud SQL, and a later forecast job's output is picked up.
- Only instances that actually hold forecasts are cached.
- A strategy that raises mid-run is replaced by bands for that location, with a warn event.

### Prices

- `PRICE_SOURCE=snapshot` (the default) uses data/snapshot through `src.feeds.parquet.snapshot_feed`.
- `PRICE_SOURCE=postgres` uses `src.feeds.replay.PostgresReplayFeed(market="RT")` over the `prices` table.
- `tests/test_fleet_price_source.py` shows both give identical runs. It skips unless Postgres holds the real RT rows.
- A run finishes (`run_finished`) once the loaded data has had no price for 3 days past the last one.

## API notes (docs/API.md, implemented as written)

**Pinned contract (docs/API.md on main, 0a2396f + 2613763 + 3000d37), as implemented:**
- `/settings` items:
  - Preset `{id, label, date_local, start_utc, start_local, description}`;
  - Strategy `{id, label, description}`;
  - Speed `{tps, label}` (`"1 hour every 6 s"`).
- WebSocket:
  - The snapshot carries `sim_time_utc` and `speed_tps`. `settings` is null while no run exists, as in the website's mock.
  - A frame goes out every second while a run exists (starting, running, paused or finished).
  - `{"type": "idle", "status": "idle", "run_id": null, "last_run": {run_id, totals} | null}` goes out every 5 s when none does. On a run-to-idle transition it is sent at once.
  - A new `run_id` gets a fresh `snapshot`.
- `POST /run/speed`: the next frame's `speed_tps` and the snapshot's `settings.speed_tps` show it. With no run it returns 409 `no_active_run`, like pause and resume.
- Events:
  - `run_finished` is emitted at the end of the loaded data, or at the end of an internal `max_ticks` run.
  - Worker events carry `detail.worker_id` (0-based) and `detail.controller` (1-based). `worker_adopted` adds `adopted_by` and `ms`.
- CORS: `allow_headers` are exactly `Content-Type` and `X-Presenter-Passcode`; `expose_headers` is `Retry-After`.
- A 429 has body `{error, message, retry_after_s}` and a `Retry-After` header.
- Bridges:
  - `POST /backtest`: a ValueError gives 400 `invalid_backtest`; while a job runs, the service returns that job's id.
  - `GET /backtest/{id}`: a KeyError gives 404 `unknown_job`. The response adds `skipped`, which defaults to `{}`.
  - `GET /results/pitch`: `ResultsNotFound` gives 404 `no_results` ("No full-year results yet.").
  - `GET /forecast?series=demand&location=ERCOT` is supported; nothing to return gives 404 `no_forecast`.
  - `backtest_done` comes from `service.add_listener`, deduped per job.

**Extra error codes (now documented in docs/API.md):**
- `no_active_run` (409)
- `simulator_unavailable` (503: Redis or the coordinator isn't answering)
- `unknown_worker` / `unknown_battery` / `unknown_job` (404)
- `not_a_worker` (409 safety guard: the registry pid isn't a `src.sim.worker` process)
- `invalid_*` (400)

**`/chaos/kill` checks, before `SIGKILL`:**
- The pid comes from the worker registry.
- The pid must not be the API, its parent, the coordinator or Redis (`INFO` `process_id`).
- The process command line must contain `src.sim.worker`: `/proc/{pid}/cmdline` on Linux, `ps` on macOS.
- A zombie counts as `already_dead`.

**Rate limits** are in-memory, which works because there is one API process and one shared fleet. Invalid `/run/start` bodies (400) don't consume the 5 s budget.

**Passcode:**
- Read per request from `PRESENTER_PASSCODE_FILE` (default `/secrets/presenter/passcode`), then `PRESENTER_PASSCODE`, then `DEMO_PRESENTER_PASSCODE` (the name `.env.example` uses).
- Compared with SHA-256 + `hmac.compare_digest`.

**Forecasts:**
- The API asks `src.forecast.provider.latest_forecast` once per location per 15-min slot of sim time.
- It emits `forecast_issued` once per issue.

**Priority:** the launcher runs the API at `nice 5` (`FLEET_API_NICE`). The full-year backtest jobs the API spawns inherit it, so a re-run of the year can't starve the coordinator and workers into missing their 250 ms deadlines on 4 vCPUs.

**Client IP** is `request.client.host`. The launcher runs uvicorn with `--proxy-headers --forwarded-allow-ips '*'` (for Cloud Run).

## Measured (M3 Pro, 12 cores; Redis 8.0.3 via scripts/dev/services.sh)

| What | Number |
|---|---|
| M2: 288 ticks × 20,000 batteries × 20 workers, max speed | **32–38 ticks/s** (7.6–8.9 s); max error 0.0%; barrier p50 23–28 ms, max 33–45 ms |
| Parity with src.backtest, one sim day at 20,000 / 20 workers | bit-identical per-battery SoC, per-tick kWh and Ledger summary (bands $14,409.261278; peak evening $100,331.855494) |
| 20,000 batteries at 10 ticks/s (the fastest visitor speed) | 9.6 ticks/s achieved; barrier p50 30 ms, max 36 ms (deadline 100 ms) |
| Boot: 20 workers spawned and heartbeating | 0.9–1.2 s |
| Run start: prices + 20k batteries initialised + all `ready` | 0.8–1.0 s |
| M3 at max speed: error by tick after SIGKILL | +1: 5.02% (the dead cohort's share, unavoidable); +2 onwards: 0.000% |
| M3 at 2 ticks/s: error by tick after SIGKILL | +1: 4.93%; +2 onwards: 0.000% |
| Handover (fence + drain + reassign 1,000 batteries) | 2.2–4 ms |
| dead_letter per kill | 2,000 `expired_owner_dead` (2 unconfirmed ticks × 1,000) |
| Memory while running | workers ~47 MB each (0.94 GB for 20); coordinator ~200 MB with the 2025 price feed; Redis ~45 MB peak |
| Write path alone (20 procs × 1,000 hashes, 5 fields) | raw RESP ~31–40 ticks/s vs redis-py pipeline ~30 |

## Test map

| File | Proves |
|---|---|
| `test_fleet_m2.py` | M2 at 20k/20: every tick within 5% (in fact 0.0%). Hashes, telemetry and a journal replay agree exactly. The ledger saw every tick. |
| `test_recovery.py` | M3 at max speed and at 2 ticks/s: DEAD on the 2nd miss, reassigned for the very next tick, within 5% from kill+2, `expired_owner_dead` entries, nothing lost or double-applied. Also covers: a frozen worker fenced out as `epoch_mismatch` when it wakes; one miss is only SUSPECT (its late energy is booked); heartbeat expiry while paused; reset. |
| `test_idempotency.py` | Real worker processes, with SoC compared exactly against one application of `battery.step` per tick: duplicate/stale redelivery, redelivery across adoption (same command ids), zombie writes, and retries including "the write landed but the reply was lost". |
| `test_fleet_parity.py` | `run_fleet_targets()` gives exactly the SoC of the in-process `allocate` + `step` loop. |
| `test_fleet_strategy_parity.py` | On the M0 day the fleet decides exactly like `src/sim/skeleton.py` (bands and peak, lag 3), with the same RT and decision prices. |
| `test_fleet_backtest_parity.py` | `run_fleet_replay()` vs `src.backtest` (seam path and bands fast path): bit-identical target, feasible target, kWh per tick, final SoC per battery, and Ledger summary. Three days: the M0 day, the demo evening and 1 Jan 2025 (warm-up in 2024). |
| `test_fleet_price_source.py` | `PRICE_SOURCE=postgres` replays exactly like the snapshot (needs Postgres with real RT rows). |
| `test_api_unit.py`, `test_api_live.py` | Every message shape in docs/API.md, availability scopes, presenter auth, rate limits, CORS, chaos-kill guards, run controls, forecasts riding along, 501 bridges. |
| `test_fleet_launcher.py` | SIGTERM → exit 0 with nothing left running; dead coordinator → exit 1; SIGKILLed launcher → no orphans. |
| `test_contracts_battery.py[redis]` | The Redis BatteryAdapter passes the frozen seam's 25 contract tests. |

## Open issues

- **Cloud Run speed is unmeasured.** It needs a check once deployed: `/state` workers, plus `barrier_ms` from `python -m src.sim.coordinator bench`. At 10 ticks/s, 20k batteries write about 200k HSET/s into one Redis thread.
- **The M0 finding reproduces live:** on the spike evening, bands sells before the 7:45 pm spike (empty by 5:30 pm). That is the strategy area's concern.
- **Postgres prices are the opt-in source.** Set `PRICE_SOURCE=postgres` on Cloud Run to read Cloud SQL; the image already ships the snapshot, which is the default.
- **The forecast strategy uses the configured fleet's duration** (`make_forecast_strategy`'s default, 20,000 batteries), even for smaller test fleets. That's what the integrator asked for (no extra context).
