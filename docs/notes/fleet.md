# Fleet (M2, M3) and backend API: notes

Owner: fleet + API builder. Spec §7, RUNLOG decisions 3–5 and the engineering defaults.

## How to run

```sh
make setup
make fleet-up            # Postgres :5442 + Redis :6442 (project-local)
make fleet-test          # fleet, recovery, idempotency, API, adapter suites against that Redis (~1 min)
make fleet-test-all      # the whole suite with Redis reachable (nothing redis-marked skipped)
make fleet-m2            # M2: 1 sim day, 20,000 batteries, 20 worker processes, max speed
make fleet-m3            # M3: SIGKILL worker 17 (600 San Antonio + 400 Austin) during max discharge, max speed
make fleet-m3-2tps       # M3 at the demo speed, 2 ticks/s
make fleet-parity        # one sim day through the fleet vs src.backtest: bit-identical (SoC, kWh, ledger), 4 cities
make fleet-serve         # whole backend on :8442 (coordinator + 20 workers + API)
make fleet-down
```

- Plain `make test` runs everything; tests marked `redis` skip cleanly when `REDIS_URL` (default `redis://127.0.0.1:6379/0`) is unreachable.
- Other ports: `make fleet-up fleet-test FLEET_PGPORT=5445 FLEET_REDIS_PORT=6445` (the Texas Triangle work used these).
- `make fleet-m3 FLEET_KILL_WORKER=7` kills another controller.
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
| `snapshot`, `events`, `ticks`, `workers`, `layout`, `kw`, `epoch`, `coordinator` | Read by the API. `workers` is the pid registry used by `/chaos/kill`. `kw` is float32 current power per battery. The snapshot also carries `restarting`, `ready` and `workers_starting` (readiness for a Start). |
| `battery_status` | Packed 4-bit status per battery at the latest tick (docs/API.md), overwritten every tick. |

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

- Battery g is `b{g:05d}` from `src.sim.fleet.generate_batteries`. `src.sim.fleet.assign_locations` gives each location a contiguous block in config order, the same split the backtest uses: at 20,000, LZ_HOUSTON is b00000–b06999, LZ_NORTH b07000–b14999, LZ_CPS b15000–b17599 and LZ_AEN b17600–b19999.
- Worker w starts with chunk w of the fleet in id order (b{1000w}–b{1000w+999} at 20k/20), so it serves one city, or two at a boundary: worker 17 holds 600 San Antonio + 400 Austin batteries.
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
- CORS: `allow_headers` is exactly `Content-Type` (presenter mode is gone, 2026-09-26); `expose_headers` is `Retry-After`.
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
- `/chaos/kill` is open to everyone (no presenter mode since 2026-09-26): one kill per 2 s, globally (`api.rate_limits.chaos_kill_s`). A malformed `worker_id` (400) doesn't spend the budget; a refused kill (409 `already_dead` / `too_few_healthy`, 404) does.

**Forecasts:**
- The API asks `src.forecast.provider.latest_forecast` once per location per 15-min slot of sim time.
- It emits `forecast_issued` once per issue.

**Priority:** the launcher runs the API at `nice 5` (`FLEET_API_NICE`). The full-year backtest jobs the API spawns inherit it, so a re-run of the year can't starve the coordinator and workers into missing their 250 ms deadlines on 4 vCPUs.

**Client IP** is `request.client.host`. The launcher runs uvicorn with `--proxy-headers --forwarded-allow-ips '*'` (for Cloud Run).

## Texas Triangle (2026-09-26): four cities, the live map

The fleet is `LZ_HOUSTON 0.35, LZ_NORTH 0.40, LZ_CPS 0.13, LZ_AEN 0.12` (config.yaml): 7,000 / 8,000 / 2,600 /
2,400 batteries in contiguous id blocks in config order (b00000–b06999, b07000–b14999, b15000–b17599,
b17600–b19999). Everything below is built against docs/API.md as written.

### What changed, in one place each

| Piece | Where | Notes |
|---|---|---|
| City names | `src/sim/geo.py` `CITIES` | THE mapping from a load zone to what people see: `Houston`/HOU, `Dallas–Fort Worth`/DFW, `San Antonio`/SA, `Austin`/AUS, plus a centre lon/lat. `control.area()`, the event messages, `GET /fleet/layout`, `GET /batteries/{id}` and the Results text (`src/backtest/results.py`) all read it. |
| Controllers' cohorts | `FleetLayout.default_owners` | **Contiguous blocks of the fleet in id order** (was: a chunk of every location per worker). So a controller serves one city, or two at a boundary: 0–6 Houston, 7–14 Dallas–Fort Worth, 15–16 San Antonio, **17 = 600 San Antonio + 400 Austin**, 18–19 Austin. docs/API.md's Worker example (worker 7 → `{"LZ_NORTH": 1000}`) and the map's "controllers serving the selected city" need this. Parity is unaffected (allocation is per location, whoever owns the batteries). |
| Prices for every city | `coordinator.price_feed(years, locations)`, `feeds.parquet` | **Bug fixed:** the snapshot feed defaulted to Houston + North, so the live fleet had no prices for San Antonio and Austin (they held all day). The feed now defaults to the fleet's locations and the coordinator passes its own. |
| Positions | `src/sim/geo.py`, `src/api/layout.py` | see "Positions" below |
| `battery_status` | `src/sim/battery_status.py`, coordinator `step()`, `Backend.battery_status` | see below |
| `totals.by_location` | `RunState.by_location`, `_add_totals` | revenue_usd, sold_kwh, bought_kwh per city over the whole run, including late telemetry booked at its own tick's price. The cities add up to the fleet totals (to float rounding). |
| `workers[].by_location` | `WorkerHandle.public(layout)` | batteries it owns **now**, per city, only cities it has (as in the API example); `{}` for a dead worker. Changes on hand-overs. |
| Start after Reset | coordinator `restarting`, API `_wait_until_ready` | see below; new error code `simulator_restarting` |
| Forecast fallback | `build_strategy_set` | **per city**, see below |

### Positions (`GET /fleet/layout`)

- Each metro is a hand-listed set of real city and suburb centres (approximate lon/lat, 12–28 per metro, e.g.
  Katy, Cypress, The Woodlands for Houston; Plano, Arlington, Denton for DFW; Stone Oak, New Braunfels for San
  Antonio; Round Rock, San Marcos for Austin), each weighted by rough population (thousands) and with its own
  Gaussian jitter (2.5–6 km per axis: bigger for the core cities).
- Battery i picks a centre with probability ∝ weight, then jitters. A point outside the metro radius (Houston
  70 km, the others 65/55/55 km around the centre) is redrawn up to 3 times, then pulled back towards its centre,
  so every battery lies inside its metro. The metros don't overlap.
- Draws come from a counter-based hash (SplitMix64 over sha256(seed, location), the battery index and a stream
  number), not a sequential RNG, so battery i's position depends only on (seed, i, its location): the same in every
  process, run and fleet size. 20,000 positions take ~3 ms.
- Encoding exactly per docs/API.md: little-endian Int16 `[lon, lat]` pairs in id order, `lon = -100 + v/1000`,
  `lat = 31 + v/1000` (≤ 0.0005° ≈ 50 m rounding), base64. `cities[]` are in config order with `batteries`,
  `first_battery`, `name`, `short` and `center_lonlat` (the hand-picked metro centre, e.g. DFW `[-96.97, 32.82]`,
  for labels; not the centroid of the dots).
- `version` = sha256 over (size, shares in order, seed, `GEO_VERSION`)[:16]. `GEO_VERSION` is there so a change to
  the site list also changes the version. Built once per version (`lru_cache`), the JSON body is cached too, and
  the response carries `ETag: "<version>"` (If-None-Match → 304) and `Cache-Control: public, max-age=300`.
- Before the simulator is up (Redis unreachable or no layout yet) it answers from config.yaml, so the map can draw
  during a cold start. Once up, size and seed come from the running fleet (tests run 300 batteries).
- `GET /batteries/{id}` adds `city` (the name) and `lonlat`, the same Int16-grid point the map draws.
- Size: 107 KB of JSON at 20,000 (106,668 base64 characters).

### `battery_status`

- Codes per battery at the latest tick, computed vectorized in `step()` from that tick's setpoints, the
  availability overrides and ownership: `0` offline override wins; else `4` if its controller was not healthy when
  the tick went out (suspect, dead, or no owner) **or** was sent the tick and didn't report it; else `2` setpoint
  > 1e-6 kW, `3` < −1e-6 kW, `1` otherwise.
- So after a SIGKILL, code 4 covers exactly the kill → hand-over window: the tick the worker missed (its batteries
  really didn't move) and the next one (it is suspect, so they got 0); at the end of that tick it is declared dead
  and re-homed, and the tick after shows them selling again. At 2 ticks/s that is ~1 s, so one frame shows it.
- Packed as docs/API.md says (byte i // 2, low nibble for even i): 10,000 bytes at 20,000. The coordinator writes
  one key (`battery_status`) in the tick's existing Redis pipeline (no extra round trip), overwritten every tick;
  the API base64s the latest into the snapshot and every frame (13,336 characters).
- Outside ticks: boot, reset and run start write the idle form (1 or 0). Since 2026-09-26 every availability change
  and every crash is published at once in every state (see "v2" below): no run, the idle form; otherwise the
  latest tick's look with the current offline and controller-down overlays (the next tick recomputes everything).
- Overhead: 0.05 ms p50, 0.1–0.6 ms max per tick at 20,000 (compute + pack; the SET rides the tick's pipeline).
- A 2 ticks/s frame at 20,000 is ~19.2 KB (13.3 KB of it `battery_status`); the snapshot with 6 ticks of history is
  ~24 KB; an idle `/state` ~17 KB.

### Start right after Reset

- Why it failed: a Reset's reply goes out first, then the coordinator respawns the 20 workers and rewrites 20,000
  hashes **blocking its loop** (seconds on 4 vCPUs). A Start sent meanwhile sat in the control stream and the API
  gave up after `control_timeout_s` (4 s): the four 503s after ~4.1 s in the Cloud Run logs.
- Now: the coordinator publishes `restarting: true` in its snapshot before the reply to a Reset (and at boot), and
  clears it once no worker is `starting` (every one has sent its first heartbeat, or was given up on after
  `spawn_timeout_s`). `POST /run/start` (after validation and the rate limit) polls the snapshot every 0.2 s for
  up to 15 s (`FLEET_START_READY_TIMEOUT_S`, or `api.start_ready_timeout_s`), then sends the Start, which the
  now idle coordinator answers at once.
- Still not ready after 15 s: `503 {"error": "simulator_restarting", "retry_after_s": 5, "message": "The simulator
  is still restarting its controllers. Please try again in 5 seconds."}` plus a `Retry-After: 5` header.
  No coordinator snapshot at all (cold start where the coordinator never came up): `503 simulator_unavailable`.
- Locally a Reset-then-Start is accepted ~0.6 s after the Reset (3 workers); the test slows the respawn to 1 s with
  a 0.5 s control timeout and fails without the wait (mutation-checked).

### Forecasts: the fallback is per city

- `forecast.locations` lists the 4 cities; San Antonio and Austin forecasts are still being computed in the cloud.
- **Live fleet: per city.** Each city gets its own forecast strategy instance, loading only that city's
  forecasts (1 read per city instead of 4, cached per city while it holds forecasts). A city whose forecasts cover
  under 90% of the first replayed day (or whose strategy can't be built) runs the simple rule; the others keep
  the AI forecast. `run_started` is then `warn`: *"Replaying real prices from May 16, 2025, 4:00 pm (Central time)
  at 1 hour of market time every 6 seconds, using the AI forecast (TimesFM-3) in Houston and Dallas–Fort Worth and
  the simple rule in San Antonio and Austin, whose AI forecasts aren't ready yet."* with
  `detail.strategy_by_location` and `detail.fallback_locations`. All four missing: the old fleet-wide message. Why
  per city: a demo started before the cloud job finishes still shows the AI where it can, and each city's
  decisions are independent anyway (one Strategy per location).
- **Backtest: all-or-nothing, every city checked.** `run_strategy` checks every city against
  `backtest.forecast_min_coverage` (99%) and raises `ForecastsMissing` naming all short cities
  (`forecasts cover 0.0% at LZ_CPS, 0.0% at LZ_AEN of 2025 (need 99% in every city)`); the Results say *"its AI
  price forecasts for San Antonio and Austin aren't ready for 2025 yet."* A headline number must be one method
  for the whole fleet. The backtest also builds one forecast instance per city now (4 reads instead of 16).
- The warm-up thread logs which cities are not ready yet.

### Wording

- City names everywhere from `geo`: *"Batteries in all four cities are now selling while prices are high."*;
  grouped when mixed (*"Houston and San Antonio batteries are now charging while power is cheap; Dallas–Fort Worth
  batteries are now waiting; ..."*); *"Prices jumped in Dallas–Fort Worth, San Antonio and Austin, up to $1,295/MWh
  ($1.29 per kWh) in San Antonio."* (cities that cross in the same tick share one event; before, each city got
  its own); *"Controller 18's 1,000 batteries in San Antonio and Austin were handed to the other 19 controllers in
  5 ms."*; *"30% of the Houston batteries (2,100) are now offline; ..."*; one `forecast_issued` per issue time
  (*"New AI price forecasts for all four cities are ready."*).
- Times say "(Central time)": all four cities keep America/Chicago time, so "Houston time" singled one out.
- Preset descriptions (config.yaml, descriptions only) talk about the four cities, checked against real prices:
  May 16 all four above $2,000 at 7:45 pm (San Antonio $3,150); Apr 7 all four above $3,500 at 6:45 am; Jul 11 all
  four ≈ $1,755 at 9 pm; Jun 28 is at the 44th–51st percentile of 2025 daily spread in every city.

### Measured at four cities (M3 Pro, 2026-09-26; Redis 8 on :6445, Postgres 16 on :5445 with 2024–2025 ingested)

| What | Number |
|---|---|
| M2: 288 ticks × 20,000 × 20 workers, max speed, bands, spike evening | **38.3 ticks/s** (7.5 s), max error **0.0%**, barrier p50 22.3 ms, max 35.3 ms |
| M3 at max speed, SIGKILL **worker 17** (600 SA + 400 AUS) during max discharge | tick +1: **4.964%**, then **0.000%** from +2; declared dead at +2; hand-over 5 ms; 2,000 `expired_owner_dead` |
| M3 at 2 ticks/s, worker 17 | tick +1: **4.964%**, then **0.000%**; hand-over 8 ms; 2,000 dead letters; 1.94 ticks/s |
| `battery_status` code 4 after the kill | ticks +1 and +2: exactly its 1,000 batteries; +3: none (each survivor now holds ~31–32 SA + 21–22 AUS) |
| Parity with src.backtest, 4 cities, 20 workers | bit-identical per-battery SoC, kWh and feasible target per tick, and Ledger summary, on 4 days (2,000 batteries: M0 day, demo evening, Jan 1; **20,000**: 2025-02-19) |
| 2025-02-19, the worker-17 day | Austin tops $951/MWh, San Antonio $389: the simple rule decides opposite directions for worker 17's two cities on 21 ticks and they act differently on 111 of 288 |
| `battery_status` per tick | 0.05 ms p50, ≤ 0.6 ms max |
| Frame at 2 ticks/s, 20,000 | ~19.2 KB |
| Full-year backtest, 2025, 20,000 × 4 cities (`make backtest-speed`) | bands 21.1 s (lag 3 + lag 0), peak 33.8 s; bands $123.67 and peak $136.70 per battery (two cities gave $119.78 and $126.58) |

## Measured before the Texas Triangle (2 cities; M3 Pro, 12 cores; Redis 8.0.3 via scripts/dev/services.sh)

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
| `test_recovery.py` | M3 at max speed (worker 17, two cities) and at 2 ticks/s (worker 13): DEAD on the 2nd miss, reassigned for the very next tick, within 5% from kill+2, `expired_owner_dead` entries, nothing lost or double-applied; `battery_status` 4 for exactly its batteries from the kill to the hand-over and not after; `by_location` moves to the adopters. Also covers: a frozen worker fenced out as `epoch_mismatch` when it wakes; one miss is only SUSPECT (its late energy is booked); heartbeat expiry while paused; reset. |
| `test_idempotency.py` | Real worker processes, with SoC compared exactly against one application of `battery.step` per tick: duplicate/stale redelivery, redelivery across adoption (same command ids), zombie writes, and retries including "the write landed but the reply was lost". |
| `test_fleet_parity.py` | `run_fleet_targets()` gives exactly the SoC of the in-process `allocate` + `step` loop. |
| `test_fleet_strategy_parity.py` | On the M0 day the fleet decides exactly like `src/sim/skeleton.py` (bands and peak, lag 3), with the same RT and decision prices. |
| `test_fleet_backtest_parity.py` | `run_fleet_replay()` vs `src.backtest` (seam path and bands fast path) at 4 cities and 20 workers: bit-identical target, feasible target, kWh per tick, final SoC per battery (worker 17's cohort checked on its own), and Ledger summary. Four days: the M0 day, the demo evening, 1 Jan 2025 (warm-up in 2024) and, at the full 20,000, 2025-02-19 when worker 17's two cities act differently on 111 of 288 ticks. |
| `test_fleet_cities.py` | Four-city event wording through the real fleet (price spikes grouped per tick, all four cities, Central time), controllers' `by_location`, per-city totals, the per-city AI-forecast fallback and its `run_started` message, `battery_status` overlaid while paused. |
| `test_geo.py`, `test_battery_status.py` | Positions: deterministic across runs, processes and fleet sizes, inside a Texas bbox and their metro radius, per-city counts, population weighting, the Int16 encoding, the version hash. Status codes: nibble order, 10,000 bytes at 20k, precedence, speed. |
| `test_fleet_price_source.py` | `PRICE_SOURCE=postgres` replays exactly like the snapshot (needs Postgres with real RT rows). |
| `test_api_unit.py`, `test_api_live.py` | Every message shape in docs/API.md (now with `battery_status`, `totals.by_location`, `workers[].by_location`, battery `city`/`lonlat`, Settings `year`, `years`, Tick `predicted`, `race.potential`), `GET /fleet/layout` (contract keys, decoding, ETag/304, from config before the simulator is up), availability scopes, rate limits (the public kill: one per 2 s), CORS (Content-Type only), chaos-kill guards, run controls, Start right after Reset, `simulator_restarting`, forecasts riding along, 501 bridges, presenter mode gone; battery_status within one frame while paused (a controller taken offline: code 0 on exactly its batteries; a crash: code 4, then the hand-over). |
| `test_years.py` | Years 2025/2026 and where 2026's real prices end (the snapshot), per-year presets (From January 1, the computed "Where real prices end"), year/preset resolution and its 400s. |
| `test_fleet_prediction.py` | 2026 through the real fleet: real prices then the prediction's p50, `predicted` per tick and in the stored history, one `prediction_started`, the AI on virtual forecasts, the replay ending at real_until without a prediction, the late-December 2025 warm-up. |
| `test_fleet_launcher.py` | SIGTERM → exit 0 with nothing left running; dead coordinator → exit 1; SIGKILLed launcher → no orphans. |
| `test_contracts_battery.py[redis]` | The Redis BatteryAdapter passes the frozen seam's 25 contract tests. |

## The live race (2026-09-26): a shadow fleet on the simple rule

docs/API.md `Race` and the `race_update` event. Code: `src/sim/race.py`, hooks in the coordinator
(`_start_race`, `_race_step`, `_race_events`, `snapshot()["race"]`), `race` in the API's snapshot and frames
(`src/api/live.py`). Tests: `tests/test_race.py` (no services), `tests/test_race_fleet.py` (Redis).

### What it is

- **Strategy:** `race.comparison_strategy(live)`: `bands` ("Simple rule"), or `forecast` when the live run's
  effective strategy is `bands`. One instance per city from `build_strategy_set` (so `make_strategy(cfg.strategy,
  tick_seconds, name)` with no extra context, as the live fleet builds them; a forecast instance loads only its
  city's forecasts, which changes no decision). The label comes from config `backtest.labels`.
- **Start state:** the same batteries, built the backtest's way (`engine.build_fleets` = `generate_batteries` +
  `assign_locations`, once per coordinator and checked id-for-id and kWh-for-kWh against the live layout), all at
  the run's `start_charge_level`.
- **Each tick,** for each city, exactly the backtest engine's steps: the Strategy through `StrategyTargets` over the
  live run's own `PriceWindow` (`decision_tick`, the same lag, the same trailing history as the live fleet), then
  `engine.deliver` (headroom_kw, allocate, battery.step, grid kWh from the SoC change: the function
  `replay_location` now calls too). The kWh are booked at that tick's RT price exactly as `BacktestLedger.record`
  books them. It steps right after the live commands go out, while the workers apply them.
- **Availability overrides are mirrored** (an offline battery has no headroom and holds its charge; H and the
  strategy's fleet_soc cover the available batteries only, as for the live fleet). **Crashes are not:** the race has
  no controllers.
- **`race` is `null`** while idle, while a run is `starting` (the second before its first tick is prepared), for
  internal fixed-target runs, and when the comparison strategy can't run in any city (a live simple rule with no AI
  forecasts anywhere, as on a laptop: there is nothing to race). If only some cities have forecasts, those race the
  AI and the others race the simple rule (both fleets are then identical there). `FLEET_RACE=0` switches it off.
- **Block** (snapshot, every frame, `GET /state`): `strategy`, `label`, `revenue_usd` (fsum of the cities, as the
  Ledger totals), `by_location` {`revenue_usd`, `charge_level`, `action`}, `charge_level` (capacity-weighted over all
  batteries, as `Tick.charge_level`), `live_minus_race_usd` = the live `totals.revenue_usd` minus the race's (same
  snapshot), and since v2 `potential` and `shows_potential` (see "v2" below). Finished and paused runs keep it.

### race_update events

- A spike begins when any city's 5-minute price reaches `api.price_spike_usd_mwh` ($1,000/MWh) and no spike is open;
  it ends once every city is below half of that (the `price_spike` event's hysteresis). One event each, so at most
  two per spike episode.
- Start: both fleets' charge levels **just before** the spike's first step. Prices within 5% of each other are shown
  as one number, weighted by each city's batteries; otherwise the highest ("as much as"). The AI is named first when
  it is in the race. Detail: `phase: "spike_started"`, the prices, both charge levels.
- End: the lead over the whole run so far ("ahead of" / "behind" / "have earned the same so far"). Severity `good`
  when the live strategy leads (by a dollar or more), `info` otherwise. Detail: `phase: "spike_ended"`, both
  revenues, what each earned during the spike, `live_minus_race_usd`.
- Labels everywhere come from config `backtest.labels`: the race's `label`, the `run_started` sentence, the
  Results, `/settings` and AI gains ("AI forecast (TimesFM-3)", "Simple rule", "Evening peak").
- Real output (live evening peak, 20,000 batteries, the summer_spike preset from 6 pm at 90%, 2 ticks/s):
  *"Prices jumped to $1,757 in all four cities. The evening-peak rule had 20% charge left; the simple rule had
  30%."* then *"The evening-peak rule is now $36,904 behind the simple rule."* With the AI live (the default) the
  same events read *"... The AI fleet had X% charge left; the simple rule had Y%."* and *"The AI is now $N ahead of
  the simple rule."* (docs/API.md's examples, word for word in `tests/test_race.py`).

### Parity with src.backtest: bit for bit

- `tests/test_race.py`: the race, driven as the coordinator drives it, equals `src.backtest.runner.replay` for the
  same window from the same start state, **exactly** (`==` on each city's revenue, the fleet total and every
  battery's final SoC): bands on the summer_spike evening at the full 20,000, on 2025-01-01 (warm-up in 2024) and on
  the spike evening, against both the bands fast path and the per-tick seam; the AI forecast (synthetic forecast
  books); and availability changing mid-run (30% of Houston and a 300-battery block of DFW offline 8-9 pm, compared
  with a backtest replayed in segments with the same masks; the offline batteries' SoC doesn't move).
- `tests/test_race_fleet.py`, through the real coordinator and workers: the race's cities equal the backtest's for
  bands bit for bit while the live evening peak runs next to it (and the live ledger equals the backtest's peak
  summary), so `live_minus_race_usd` = backtest(peak) - backtest(bands); the 9 pm spike's two events with the right
  times, words and charge levels; availability mirrored while a SIGKILLed controller is not; no race without
  forecasts; the AI racing a live simple rule.

### Overhead at 20,000 batteries (M3 Pro)

| | p50 | p99 | max |
|---|---|---|---|
| Race step alone, in-process (`test_race.py`), bands / forecast | 0.36 / 0.16 ms | 0.82 / 0.62 ms | 0.86 / 0.67 ms |
| Inside the live loop at max speed (bench, 288 ticks, race = bands) | 0.61-0.65 ms | 1.7-1.8 ms | 1.8 ms |
| Inside the live loop at 2 ticks/s (the demo speed; cold CPU between ticks) | 1.8-2.0 ms | 5.7 ms | 6.3 ms |

- The loop doesn't slow down: A/B with `FLEET_RACE=0` at max speed, 40.65 vs 40.40 and 41.38 vs 41.14 ticks/s
  (noise); the telemetry barrier p50 stays ~21.5 ms, because the race steps inside the barrier wait. At 2 ticks/s
  the barrier p50 was 41 ms without and 34 ms with it (noise).
- Cloud Run (4 vCPU) ran the M2 bench about 1.8x slower than the laptop, so expect ~1-4 ms per tick there, next to
  the 250 ms deadline. To measure it: point `fleet-sim-bench` at the new image and run
  `python -m src.sim.coordinator bench --ticks 288 --speed max --strategy peak` (the bench now prints `race_ms_p50`,
  `race_ms_p99`, `race_ms_max`, the race block and the race events). `peak` live means the race runs the simple
  rule, the race of the default demo.

### Requests for integrator (race)

1. docs/API.md Race: `race` is also `null` while a run is `starting` and when there is nothing to race (a live
   simple rule with no AI forecasts in any city); with forecasts for only some cities, the other cities race the
   simple rule against itself.
2. docs/API.md `race_update`: the end event's severity is `good` when the live strategy leads, else `info`; `detail`
   carries `phase` (`spike_started` / `spike_ended`), the charge levels or revenues, and `live_minus_race_usd`.
3. `FLEET_RACE=0` switches the race off without a code change (a new Cloud Run revision with that env var).

## v2 (2026-09-26): public crashes, years 2025/2026, the AI-predicted 2026, race.potential, fresh battery_status

Built against docs/API.md as of 4879fde. Code: `src/data/years.py`, `src/feeds/predicted.py`,
`src/forecast/predictions.py`, `src/forecast/predict.py`, migration 030, and changes in `src/sim/control.py`,
`src/sim/coordinator.py`, `src/sim/race.py`, `src/api/*`, `src/forecast/{job,provider}.py`.

### Presenter mode is gone

- Deleted `src/api/presenter.py`, `POST /presenter/check`, the passcode lookup (`PRESENTER_PASSCODE_FILE`,
  `PRESENTER_PASSCODE`, `DEMO_PRESENTER_PASSCODE`), the wrong-passcode limiter and its config, its tests, the
  `/secrets/presenter/passcode` mount in `scripts/cloud/deploy.sh` and the `.env.example` line (`.env` untouched).
- `POST /chaos/kill` is public: the guards stay (409 `already_dead`, `too_few_healthy`; the pid checks), plus one kill
  per 2 s globally (429 with `retry_after_s` and `Retry-After`, like the other limits). CORS allows only `Content-Type`.
- Reset still restarts every controller (killed ones included) and brings every battery online.

### Years and presets

- config `years: {default: 2025, items: [2025, 2026]}`; every preset has a `year`. `GET /settings` adds `years`:
  `{year, label, description, real_until_utc}` for each configured year with real prices for every fleet city.
- `real_until_utc` (`src/data/years.py`) = the END of the last RT interval present for ALL four fleet cities in the
  ERCOT year (one SQL over `prices`, or the snapshot files; cached 60 s per process; `PRICE_SOURCE` picks the
  source, as for the live fleet's prices). `null` when the year is real all year (2025).
- Description: "Real ERCOT prices for the whole year." / "Real prices through Sep 19, then the AI's prediction."
  (the date is the last local day with real prices), or "... The AI's prediction for the rest of the year isn't
  ready yet." when no stored prediction covers the cutoff.
- Presets: the five 2025 ones plus `jan1_2025` ("From January 1", 2025-01-01T06:00Z); for 2026
  `real_prices_end_2026` ("Where real prices end": 6 pm Central on the evening before real_until_utc, computed, so
  it follows each ERCOT publication: 2026-09-19T23:00Z with the Sep 20 files, 72 ticks = 36 s at 2 ticks/s before
  the switch), `spike_2026` (2026-04-24, from 4 pm CDT: at 7:45 pm Houston $1,051.54, Dallas–Fort Worth
  $1,231.82, San Antonio $1,096.97, Austin $1,105.20, the biggest 2026 evening where all four spike) and
  `jan1_2026` (2026-01-01T06:00Z; its bands window warms up on real Dec 25-31, 2025 prices).
- Settings: `year` is editable. A preset sets its year; a year alone replays that year's default preset
  (`presets.default_by_year`: 2025 `ai_best_evening`, 2026 `real_prices_end_2026`); a preset of another year than
  the one sent is 400 `invalid_preset`; a year that isn't configured, isn't a whole number, or has no prices loaded
  is 400 `invalid_year`. Presets of a year without prices are left out of `GET /settings`.

### Replay across real_until_utc

- `coordinator.run_feed`: a run's feed is `PredictedFeed(real, cutoff, paths)` where cutoff =
  `years.real_until_ahead(start)` (the first year from the start on whose real prices end early). Before the
  cutoff, real ticks (Postgres or the snapshot) untouched; from it on, the stored prediction's p50 (latest cutoff
  at or before real_until), forward-filled to its three 5-minute ticks, rounded to cents, no demand/fuel mix.
- `Tick.predicted` = `ts >= real_until_utc` (in frames and in the stored history). `prediction_started` goes out once,
  at the first predicted tick, with the contract's sentence and `detail.real_until_utc`, `model`, `locations`.
  A run that starts after the cutoff announces it at its first tick.
- No prediction stored: the replay ends at the cutoff, and `run_finished` says "the end of the real prices ...;
  the AI's prediction for the rest of the year isn't ready yet". `run_started` says where real prices end when the
  run's year has a cutoff.
- The simple rule and the evening peak just trade on the predicted prices. The AI forecast strategy gets VIRTUAL
  forecasts after the cutoff (docs/notes/forecast.md): hourly, 24 h, p10/p50/p90 sliced from the same prediction.
  It sees its own long-range prediction, which is also the price path it trades on, so after the cutoff its
  forecast is exact by construction and its projected earnings flatter it (docs/UX.md labels them "projected").
- `GET /forecast` and the frames' `forecast` serve the virtual forecast after the cutoff (the band moves hourly); the
  API sends no `forecast_issued` event for virtual ones (they are slices, not new forecasts).

### race.potential

- A second shadow fleet, `run.potential` (`src/sim/race.py` `Race`, stepped right after the race while the workers
  apply the live tick): the LIVE run's own Strategy instances (pure) through the live `StrategyTargets` (same prices,
  lag, history), its own SoC, every battery online, no crashes. Label: the live label without its bracket plus ",
  all batteries online" ("AI forecast, all batteries online").
- It books revenue per tick, city by city in config order (`running_usd`), exactly as the live ledger adds up
  `totals.revenue_usd`, so with no outage it equals the live revenue bit for bit: `tests/test_race_fleet.py` for
  the evening peak, the simple rule and the AI (synthetic books), and the 20,000-battery bench (shortfall 0.0 over
  288 ticks). `shortfall_usd` = potential - live.
- `shows_potential` is set at run start when batteries are already offline, and flips true (for the rest of the
  run) when an availability change leaves any battery offline, a controller misses a tick (suspect), dies, or its
  process is seen to exit. A new run starts over.
- The shortfall can be negative: an outage that happens to hold charge back for a later spike earns more (bench
  below: -$0.36 after a kill at 7:40 pm, before the 9 pm spike).
- `potential` is `null` only if that fleet couldn't start or failed; `race` itself stays `null` as before (idle,
  starting, nothing to race).

### battery_status within one frame, paused included

- The human saw a controller taken offline and no red on the map. The backend path was already publishing code 0
  (the next tick while running, at once while paused), so the likeliest cause is the v1 map drawing offline dim
  gray (docs/UX.md v2 makes it a red ring). What was really missing: **a crash while paused never showed**. The
  coordinator only notices a dead controller at heartbeat expiry while paused, then hands its batteries over at
  once, so code 4 never appeared.
- Now the coordinator publishes an overlay (`_publish_overlay`) at once on every availability change (every state),
  when it reaps an exited worker (within its 0.2 s supervision, running or paused) and after a hand-over outside a
  tick: the latest tick's look, then code 4 for batteries of a controller that is suspect, dead or has exited and
  hasn't handed them over, then code 0 for offline ones. A battery that stopped (offline or down) looks "waiting"
  until the next tick commands it. Inside a tick nothing changes: `step()` still writes that tick's codes (the M3
  code-4 window is unchanged: the missed tick and the hand-over tick).
- While paused a crash shows as code 4 ~0.2-0.4 s after the kill and stays until the heartbeat expires (6 s in
  production), then the batteries are handed over and look normal again. Running, code 4 now starts within
  ~0.2 s of the kill instead of at the next tick's barrier.
- Tests (`tests/test_api_live.py`, through the API's own frame builder and the WebSocket): paused, a controller
  taken offline gives code 0 on exactly its 100 batteries in the next frame and leaves every other battery as it
  was; a crash while paused gives code 4 on exactly its batteries within a frame (0.24 s here), then the
  hand-over. Both fail with the overlay switched off (mutation-checked).

### Measured (M3 Pro; Redis :6447, Postgres :5447 with ERCOT 2024-2026 ingested)

| What | Number |
|---|---|
| Potential step at 20,000 batteries, max speed (bench, 288 ticks, live peak) | p50 **0.17 ms**, p99 1.39 ms, max 1.57 ms (race: 0.74 / 1.87 / 2.50 ms) |
| Same at 2 ticks/s (48 ticks, a kill at tick 20) | p50 1.37 ms, p99 4.5 ms, max 6.0 ms (race 2.0 / 6.1 / 7.4 ms); deadline 250 ms |
| Loop speed with race + potential, 20,000 × 20 workers, max speed | 38.1 ticks/s, max error 0.0% (38.3 before) |
| Potential vs live, no outage, 20,000 batteries, 288 ticks | shortfall **0.0** (bit for bit) |
| M3 at 2 ticks/s with the potential, kill worker 7 at tick 20 | 5.08% on the kill tick, then 0.000%; code 4 on ticks 21-22 for its 1,000 batteries |
| Crash while paused → code 4 in a frame | 0.24 s after the kill (test) |
| End to end, the whole backend (`src.launcher`, 20,000 batteries, `PRICE_SOURCE=postgres`), 2026 "Where real prices end" at 10 ticks/s, AI live | `run_started` names the cutoff; `prediction_started` at 2026-09-20T05:00Z; the first predicted tick's prices are the p50 ($30.57–31.22); hourly virtual forecasts in the frames; by 4 am the AI $13,453.31 vs the simple rule $9,889.80, potential = live exactly (shortfall 0.0) |
| Same run, paused: worker 7 offline | the next frame: code 0 on exactly its 1,000 batteries, the other 19,000 unchanged, `shows_potential` true |
| Same run, paused: `POST /chaos/kill?worker_id=12` (no passcode), then another at once | 200, then 429 `rate_limited` (retry 2 s); code 4 on its 1,000 batteries in a frame 0.97 s after the kill (1 s frames); declared dead 5.0 s after (6 s TTL), then code 4 on none |

## Open issues

- **Cloud Run speed is unmeasured.** It needs a check once deployed: `/state` workers, plus `barrier_ms` from `python -m src.sim.coordinator bench`. At 10 ticks/s, 20k batteries write about 200k HSET/s into one Redis thread.
- **The M0 finding reproduces live:** on the spike evening, bands sells before the 7:45 pm spike (empty by 5:30 pm). That is the strategy area's concern.
- **Postgres prices are the opt-in source.** Set `PRICE_SOURCE=postgres` on Cloud Run to read Cloud SQL; the image already ships the snapshot, which is the default.
- **The forecast strategy uses the configured fleet's duration** (`make_forecast_strategy`'s default, 20,000 batteries), even for smaller test fleets. That's what the integrator asked for (no extra context).
- **The code-4 flash lasts 2 ticks** (the missed tick and the suspect one): ~1 s at 2 ticks/s, so one 1 s frame shows it, but only 0.4 s at 5 and 0.2 s at 10 ticks/s, where a frame can miss it. See the requests below.
- **The snapshot's 2,000-tick history grows with the cities:** a Tick with four cities is ~0.9 KB, so a full history is ~1.8 MB on connect (it was ~1.2 MB with two). No compression in the app.
- **Hand-over time** measured 5–8 ms in these runs (3–7 ms for worker 7 in repeated runs), vs 2–4 ms in the earlier notes; the timed code didn't change, so this looks like run-to-run noise (it includes draining pending telemetry).
- **`src/forecast/evaluate.py`** (the forecast area's research evaluation) still compares strategies on two cities with its own half/half split. It isn't on any live or backtest path.
- **A stale test was fixed on the way:** `test_ingest_parse.py`'s native-load check (it only runs when `data/raw` holds the ERCOT files, as after `--cache-dir data/raw`) still expected the snapshot's old swapped fall-back hour, which data/snapshot/README.md says was corrected.

## Requests for integrator

1. **New error code for docs/API.md:** `POST /run/start` → `503 {"error": "simulator_restarting", "retry_after_s": 5, "message": "The simulator is still restarting its controllers. Please try again in 5 seconds."}` with a `Retry-After` header, after waiting up to ~15 s for a Reset's (or a cold start's) controllers. The doc says only a 429 carries `retry_after_s`/`Retry-After`; this 503 does too. A Start with no coordinator at all still ends in `503 simulator_unavailable` (now after the same wait).
2. **Controllers now serve one city each** (contiguous id blocks; worker 17 = 600 San Antonio + 400 Austin), as docs/API.md's Worker example and the map's "controllers serving the selected city" need. RUNLOG's M2/M3 text ("500 + 500" per controller, "SIGKILL worker 7" = half Houston, half DFW) is out of date: killing worker 7 now takes out 1,000 Dallas–Fort Worth batteries. Decision 2 in RUNLOG ("LZ_HOUSTON and LZ_NORTH, 50/50") is superseded by config.yaml.
3. **Code 4 visibility above 2 ticks/s** (see Open issues). Options: (a) the map also flashes a controller's cities on `worker_suspect`/`worker_dead` (the events carry `detail.worker_id`; `worker_adopted` and `worker_suspect` now also carry `detail.by_location`), or (b) change the contract so the API reports 4 when any tick since that connection's previous frame had it. I kept "the latest tick", as specified.
4. **`idle` messages have no `battery_status`** (as the contract says), so "take offline" while idle reaches a viewer only through a new snapshot or `GET /state`, although the coordinator updates the codes at once. Either allow `battery_status` in `idle`, or the web refetches `/state` after its own `POST /availability`.
5. **docs/UX.md still speaks of two areas** ("Houston area", "Dallas–Fort Worth area", "Houston & Dallas–Fort Worth" on the Start card, "Houston time" on the clock). Events now use the layout's city names and say "(Central time)", which all four cities share.
6. **`workers[].by_location` lists only cities the controller has** (the API example shows `{"LZ_NORTH": 1000}`); a dead controller has `{}`. `totals.by_location` always has all four cities. Say if the web wants all four keys in both.
7. **`cities[].center_lonlat` is a hand-picked metro centre** (DFW is the doc's `[-96.97, 32.82]`), meant for labels, not the centroid of the dots. The layout `version` also hashes `GEO_VERSION`, so a change to the site list invalidates cached layouts.
8. **Forecasts:** the live fallback is per city (documented above); the full-year pitch needs all four cities' forecasts at ≥ 99% or it skips "forecast" and names the cities. Per the coordinator, the San Antonio and Austin forecasts are now in Cloud SQL, so in the cloud all four cities run the AI forecast; the per-city fallback still shows locally (no forecasts here) and for any replay date a city's forecasts don't cover.
9. **Pitch numbers at four cities.** The cloud's 4-city 2025 backtest (per the coordinator): AI forecast $190.95, evening peak $136.70, simple rule $123.67 per battery, ceiling $421.29. The local full-year run here gives the same bands and peak figures to the cent from the snapshot prices, which cross-checks the four-city split. RUNLOG's M4/M6 tables still show the two-city numbers.

### Requests for integrator (v2, 2026-09-26)

10. **Presenter leftovers outside my lane:** DEMO.md steps 1 and 3 (the passcode command, "Click Presenter mode") and RUNLOG's presenter lines are out of date. The Secret Manager secret `fleet-sim-presenter-passcode` (and the service account's accessor binding on it) is no longer read by anything; delete it once the new revision is live if you like.
11. **docs/API.md `race.shortfall_usd` can be negative:** an outage can hold charge back for a later spike (measured: -$0.36). Suggest one line: "It can be negative when an outage happened to save charge for a later, higher price."
12. **docs/API.md `race.potential` can be `null`** if that shadow fleet couldn't start (never seen; a failure stops it rather than the fleet), like `race` itself.
13. **docs/API.md, the AI after the cutoff:** worth one sentence under `predicted`: the AI forecast strategy then acts on hourly slices of the same prediction (its forecast is exact by construction), so its projected money after real_until flatters it. docs/notes/forecast.md has the detail.
14. **docs/API.md `forecast_issued`:** after real_until_utc the frames' `forecast` still updates hourly (virtual forecasts), but no `forecast_issued` event is sent for them. Say if the web wants them.
15. **docs/API.md `Year`:** a configured year with no prices loaded (e.g. 2026 in a database not yet ingested) is left out of `years`, and its presets out of `presets`; a Start asking for it is 400 `invalid_year` (a new code for the error table, as `invalid_*`).
16. **The weekly refresh:** ERCOT republishes 2026 about weekly. After each re-ingest (`INGEST_YEARS=2026 scripts/cloud/jobs.sh ingest`), re-run `YEAR=2026 scripts/cloud/jobs.sh forecast` (it resumes: only the new days) and `scripts/cloud/jobs.sh predict` (a new cutoff adds a new path). The "Where real prices end" preset and `years` follow real_until by themselves (cached 60 s).

