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
| `battery:{id}` | Hash, the source of truth. Fields: `soc, last_command_id, last_tick, owner_epoch, available, owner, run_id, battery_id, location, idx, capacity_kwh, power_kw, efficiency`. The dynamic fields come first, because listpack hashes are scanned linearly. |
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

### Ledger

- Grid-side delivered kWh from telemetry, per (tick, location), priced at that tick's RT price. A missing cohort counts as 0 for that tick's `actual_kw`.
- Late telemetry, from a SUSPECT worker that did apply its commands, is booked additively at its own tick's price, because that energy really moved. So the running totals can exceed the sum of per-tick `revenue_usd` by that amount.

### Run start

1. The coordinator sends each live worker a `load` message.
2. The worker initialises its batteries (`soc0`, empty fence, `run_id`) in its own fenced transaction. Doing it in the worker, ordered after any in-flight old-run batches in its stream, prevents a lagging worker from clobbering the new run.
3. The coordinator waits for every `ready`, then ticks.

### Strategies

- One instance per location, from `make_strategy`.
- `forecast` tries `make_strategy(..., "forecast")`, then `src.strategy.forecast.make_forecast_strategy` / `ForecastStrategy`. Otherwise it falls back to bands, and `run_started` is sent with severity `warn`.
- A strategy that raises mid-run is replaced by bands for that location, with a warn event.

## API notes (docs/API.md, implemented as written)

**Undefined shapes I had to pick:**
- Choice = `{"value", "label", "description"}`. Strategies also carry `"available"`.
- The `idle` message is `{"type": "idle", ...}` with the same fields as a frame and `ticks: []`.
- When `run_id` changes, the connection gets a fresh `snapshot` instead of a frame.

**Extra error codes:**
- `no_active_run` (409)
- `simulator_unavailable` (503: Redis or the coordinator isn't answering)
- `unknown_worker` / `unknown_battery` (404)
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
- `backtest_done` is emitted the first time `GET /backtest/{id}` sees `done`.

**Client IP** is `request.client.host`. The launcher runs uvicorn with `--proxy-headers --forwarded-allow-ips '*'` (for Cloud Run).

## Measured (M3 Pro, 12 cores; Redis 8.0.3 via scripts/dev/services.sh)

| What | Number |
|---|---|
| M2: 288 ticks × 20,000 batteries × 20 workers, max speed | **32–35 ticks/s** (8.3–8.9 s); max error 0.0%; barrier p50 26 ms, max 36–45 ms |
| Boot: 20 workers spawned and heartbeating | 0.9–1.2 s |
| Run start: prices + 20k batteries initialised + all `ready` | 0.8–1.0 s |
| M3 at max speed: error by tick after SIGKILL | +1: 5.02% (the dead cohort's share, unavoidable); +2 onwards: 0.000% |
| M3 at 2 ticks/s: error by tick after SIGKILL | +1: 4.93%; +2 onwards: 0.000% |
| Handover (fence + drain + reassign 1,000 batteries) | 2.2–3.7 ms |
| dead_letter per kill | 2,000 `expired_owner_dead` (2 unconfirmed ticks × 1,000) |
| Memory while running | workers ~47 MB each (0.94 GB for 20); coordinator ~200 MB with the 2025 price feed; Redis ~45 MB peak |
| Write path alone (20 procs × 1,000 hashes, 5 fields) | raw RESP ~31–40 ticks/s vs redis-py pipeline ~30 |

## Open issues

- **Cloud Run speed is unmeasured.** It needs a check once deployed: `/state` workers, plus `barrier_ms` from `python -m src.sim.coordinator bench`. At 10 ticks/s, 20k batteries write about 200k HSET/s into one Redis thread.
- **The M0 finding reproduces live:** on the spike evening, bands sells before the 7:45 pm spike (empty by 5:30 pm). That is the strategy area's concern.
- **`PRICE_SOURCE=postgres` is not tested.** It imports `src.feeds.replay.PostgresReplayFeed` lazily and calls `PostgresReplayFeed()` with no arguments.
