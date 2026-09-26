# Mock API (development and tests only)

A stand-in for the fleet-sim backend so the website can be built and tested before, and without, the real
one. It implements the REST endpoints and the WebSocket `/live` messages from [`docs/API.md`](../../docs/API.md),
including the pinned details: `worker_id` 0–19 shown as "Controller N+1", battery ids `b00000`–`b19999`, the
`GET /settings` item shapes, the idle message every 5 s, `run_finished`, the worker event details, and the live
map fields: `GET /fleet/layout`, `battery_status`, `totals.by_location`, `workers[].by_location` and battery
`city`/`lonlat`.

## What is real and what is simulated

| | |
|---|---|
| **Real** | Electricity prices: ERCOT real-time settlement point prices for the four Texas Triangle load zones (LZ_HOUSTON, LZ_NORTH, LZ_CPS, LZ_AEN), 15-minute intervals, for every replay preset's local date and the day after (`data/ercot_rt_lz_preset_days_2025.json`, 192 rows per preset). |
| **Simulated** | Everything else: the 20,000 batteries, where they sit and their physics, the 20 controllers and their crashes, decisions of the three "strategies", power, money, charge levels, failed commands, availability, forecasts (not TimesFM), events, and every number on the Results screen. None of it is a real result. |

The site shows a "Practice data" banner whenever it is connected to this mock (its `GET /health` version starts
with `mock`), the mock's forecasts are labelled `mock (simulated, not TimesFM)`, and its full-year results list
"SIMULATED" as their first assumption.

The presets mirror `config.yaml` (`presets.items`; `summer_spike` is the default, as there):

| Preset | Replay starts (Central time) | Real prices loaded |
|---|---|---|
| `spike_evening` | May 16, 2025, 4 pm | May 16–17 |
| `morning_spike` | Apr 7, 2025, 3 am | Apr 7–8 |
| `summer_spike` (default) | Jul 11, 2025, 6 pm | Jul 11–12 |
| `ordinary_day` | Jun 28, 2025, midnight | Jun 28–29 |

The price fixture is exported from the committed snapshot `data/snapshot/ercot_rt_2025_hubs.parquet` (ERCOT MIS
report 13061, see `data/snapshot/README.md`), reading the presets and the load zones (`fleet.locations`) from
`config.yaml`. To regenerate it after either changes, from the repo root after `make setup`:

```sh
uv run python web/mock/export_prices.py      # or: make web-prices
```

A preset without loaded prices answers **501** `not_in_mock` on `POST /run/start`.

## The fleet

- **Cities**, in battery id blocks (config order, same split as `src/sim/fleet.location_counts`): Houston
  `b00000`–`b06999` (7,000), Dallas–Fort Worth `b07000`–`b14999` (8,000), San Antonio `b15000`–`b17599` (2,600),
  Austin `b17600`–`b19999` (2,400).
- **Controllers**: like the backend's `FleetLayout.default_owners`, the fleet in id order is split into 20
  contiguous blocks of 1,000: controllers 1-7 run Houston, 8-15 Dallas–Fort Worth, 16-17 San Antonio, 18 runs 600
  San Antonio and 400 Austin, 19-20 Austin (`workers[].by_location` lists only the cities a controller holds, and
  is empty once it is dead). A crashed controller's batteries go round-robin, in id order, to the healthy ones.
- **Positions** (`GET /fleet/layout`, `layout.ts`): seeded Gaussian blobs around real places in each metro
  (downtowns, suburbs, satellite cities), kept inside the Texas outline, rounded to 0.001° like the API. The same
  fleet always gets the same map.
- **`battery_status`** in every snapshot and frame, as of the latest tick (like the backend): 0 offline, 1 waiting,
  2 selling, 3 charging (from each battery's setpoint), 4 for the two ticks its controller misses before the
  hand-over. So at 5-10 ticks a second a frame can miss code 4, as with the backend; the site then flashes from
  the worker events. While paused, a controller that stops responding shows as 4 until it is handed over.
- **Money per city**: `totals.by_location` holds revenue and energy per load zone; they add up to the totals.

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
  every 5 s; `last_run` holds the id and totals of the run that was reset or replaced. Idle messages carry no
  `battery_status`, so after an availability change with no run the site fetches `GET /state` to redraw the map.
- `GET /fleet/layout` sends its version as an `ETag` and answers a matching `If-None-Match` with 304.
- Rate limits match docs/API.md: start and reset share one call per 5 s, availability 10 per second, wrong
  passcodes 5 per minute; limited calls answer 429 with `retry_after_s` and `Retry-After`.
- A crashed controller misses two 5-minute steps: at the first it is "not responding", at the second it is marked
  crashed; its batteries miss both steps' orders (those become failed commands) and show as code 4, and are handed
  over after the second. Worker events carry `detail.worker_id`, `detail.controller` and, for `worker_suspect`
  and `worker_adopted`, `detail.by_location`; `worker_adopted` adds `adopted_by` and `ms`. While paused,
  detection uses wall-clock heartbeats instead (2 s, then 6 s), which is the easiest way to watch the flash: pause,
  crash a controller, wait.
- Cities that switch action at the same step share one event ("Batteries in all four cities started selling at
  about $1,757/MWh"), and so do price spikes.
- `POST /backtest` while a job runs returns that job's id; job status includes `skipped` (always empty here).

## Files

| File | Purpose |
|---|---|
| `sim.ts` | The simulated run (pure; unit-tested in `sim.test.ts`) |
| `layout.ts` | Simulated battery positions for `GET /fleet/layout` |
| `api.ts` | HTTP routes, errors, rate limits, WebSocket broadcast, CORS |
| `server.ts` | Standalone Node server (`npm run mock`) |
| `vitePlugin.ts` | Mounts the same API inside the Vite dev server |
| `export_prices.py` | Writes the real-price fixture from `data/snapshot` for every preset and load zone in `config.yaml` |
| `data/` | The real-price fixture, with its source note inside |
