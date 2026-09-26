# Mock API (development and tests only)

A stand-in for the fleet-sim backend so the website can be built and tested before, and without, the real
one. It implements the REST endpoints and the WebSocket `/live` messages from [`docs/API.md`](../../docs/API.md),
including the pinned details: `worker_id` 0–19 shown as "Controller N+1", battery ids `b00000`–`b19999`, the
`GET /settings` item shapes, the idle message every 5 s, `run_finished`, and the worker event details.

## What is real and what is simulated

| | |
|---|---|
| **Real** | Electricity prices: ERCOT real-time settlement point prices for load zones LZ_HOUSTON and LZ_NORTH, 15-minute intervals, for every replay preset's local date and the day after (`data/ercot_rt_lz_preset_days_2025.json`, 192 rows per area per preset). |
| **Simulated** | Everything else: the 20,000 batteries and their physics, the 20 controllers and their crashes, decisions of the three "strategies", power, money, charge levels, failed commands, availability, forecasts (not TimesFM), events, and every number on the Results screen. None of it is a real result. |

The site shows a "Practice data" banner whenever it is connected to this mock (its `GET /health` version starts
with `mock`), the mock's forecasts are labelled `mock (simulated, not TimesFM)`, and its full-year results list
"SIMULATED" as their first assumption.

The presets mirror `config.yaml` (`presets.items`):

| Preset | Replay starts (Houston time) | Real prices loaded |
|---|---|---|
| `spike_evening` (default) | May 16, 2025, 4 pm | May 16–17 |
| `morning_spike` | Apr 7, 2025, 3 am | Apr 7–8 |
| `summer_spike` | Jul 11, 2025, 6 pm | Jul 11–12 |
| `ordinary_day` | Jun 28, 2025, midnight | Jun 28–29 |

The price fixture is exported from the committed snapshot `data/snapshot/ercot_rt_2025_hubs.parquet` (ERCOT MIS
report 13061, see `data/snapshot/README.md`), reading the presets from `config.yaml`. To regenerate it after the
presets change, from the repo root after `make setup`:

```sh
uv run python web/mock/export_prices.py      # or: make web-prices
```

A preset without loaded prices answers **501** `not_in_mock` on `POST /run/start`.

## Never shipped

The mock lives only in `web/mock/`. `vite.config.ts` imports it dynamically and only for the dev server
(`vite` without `VITE_API_BASE_URL`), so nothing under `src/` references it. After every production build,
`scripts/check-dist.mjs` searches `dist/` for markers from these files and fails the build if it finds one.

## Running it

- **Inside the dev server** (default): `npm run dev` serves it at `http://localhost:5173/__mock`.
- **Standalone**: `npm run mock` serves it at `http://localhost:8787` (set `MOCK_PORT` to change), with CORS open
  to any origin. Point a build at it with `VITE_API_BASE_URL=http://localhost:8787`.

Knobs (environment variables, for either mode):

| Variable | Effect |
|---|---|
| `MOCK_PRESENTER_PASSCODE` | Presenter passcode. Default `presenter` (a test value for this mock only). Set it to an empty string to test "passcode not set" (503). |
| `MOCK_COLD_START_MS` | Hold every request (and refuse WebSockets) for this long after start, like a Cloud Run cold start. Try `8000` to see "Waking up the simulator…". |
| `MOCK_WS=off` | Refuse WebSocket upgrades, so the site falls back to polling `GET /state`. |
| `MOCK_NO_RESULTS=1` | `GET /results/pitch` answers 404 `no_results` until a full-year re-run finishes. |
| `MOCK_BACKTEST_FAIL=1` | Full-year re-runs end with status `failed`. |

## Behaviour worth knowing

- A replay runs until the end of its two loaded days, then reports status `finished` and a `run_finished` event.
- While no run exists, the WebSocket sends `{"type": "idle", "status": "idle", "run_id": null, "last_run": …}`
  every 5 s; `last_run` holds the id and totals of the run that was reset or replaced.
- Rate limits match docs/API.md: start and reset share one call per 5 s, availability 10 per second, wrong
  passcodes 5 per minute; limited calls answer 429 with `retry_after_s` and `Retry-After`.
- A crashed controller misses one 5-minute step ("not responding": its batteries miss their orders, and those
  orders become failed commands), then is marked crashed and its batteries are handed round-robin to the healthy
  ones. Worker events carry `detail.worker_id` and `detail.controller`; `worker_adopted` adds `adopted_by` and
  `ms`. While paused, detection uses wall-clock heartbeats instead (2 s, then 6 s).
- Batteries `b00000`–`b09999` are in the Houston area and `b10000`–`b19999` in Dallas–Fort Worth; worker N starts
  with batteries N×1000 to N×1000+999.
- `POST /backtest` while a job runs returns that job's id; job status includes `skipped` (always empty here).

## Files

| File | Purpose |
|---|---|
| `sim.ts` | The simulated run (pure; unit-tested in `sim.test.ts`) |
| `api.ts` | HTTP routes, errors, rate limits, WebSocket broadcast, CORS |
| `server.ts` | Standalone Node server (`npm run mock`) |
| `vitePlugin.ts` | Mounts the same API inside the Vite dev server |
| `export_prices.py` | Writes the real-price fixture from `data/snapshot` for every preset in `config.yaml` |
| `data/` | The real-price fixture, with its source note inside |
