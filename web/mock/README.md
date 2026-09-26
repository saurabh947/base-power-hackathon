# Mock API (development and tests only)

A stand-in for the fleet-sim backend so the website can be built and tested before, and without, the real
one. It implements the REST endpoints and the WebSocket `/live` messages from [`docs/API.md`](../../docs/API.md),
including the pinned details: `worker_id` 0–19 shown as "Controller N+1", battery ids `b00000`–`b19999`, the
`GET /settings` item shapes, the idle message every 5 s, `run_finished`, the worker event details, the live
map fields (`GET /fleet/layout`, `battery_status`, `totals.by_location`, `workers[].by_location` and battery
`city`/`lonlat`), the race (`race` in snapshots and frames, with `potential` and `shows_potential`, and
`race_update` events), the years (`years` and each preset's `year` in `GET /settings`, `year` in settings,
`predicted` ticks and the `prediction_started` event), the public `POST /chaos/kill` and `GET /results/ai-gains`,
and the failure model: each worker's `charge_level` and `usable_charge`, a crash that empties a controller's
batteries and stays down, `POST /workers/{worker_id}/restart` with `worker_recovered`, and a controller taken
offline losing half its usable charge.

## What is real and what is simulated

| | |
|---|---|
| **Real** | 2025 electricity prices: ERCOT real-time settlement point prices for the four Texas Triangle load zones (LZ_HOUSTON, LZ_NORTH, LZ_CPS, LZ_AEN), 15-minute intervals, for every 2025 replay preset's local date and the day after (192 rows each), plus September 14–20, 2025 (672 rows), in `data/ercot_rt_lz_preset_days_2025.json`. |
| **Stand-ins** | 2026's "real" prices: the mock has no 2026 data, so its 2026 replays use 2025's real prices from the same dates, a year on (January 1–2, and September 14–20 as 2026's last real week). |
| **Copied from the real backtest** | The measured full-year numbers in RUNLOG.md ("What the AI adds, measured" and the Texas Triangle results), served as the backend would, not computed here (`gains.ts`): all of `GET /results/ai-gains` (2025 only, the pitch year: the site shows no other year), and in `GET /results/pitch` the per-battery figures of the three methods, the hindsight ceiling and the AI forecast's per-city figures. |
| **Simulated** | The AI's 2026 prediction after the real prices end (2026-09-21T05:00Z, midnight on Sep 21 in Texas): for each 15 minutes, the median of the same time of day over the last 7 real days, pulled a little towards their mean and smoothed over an hour, for two days. Plausible practice data, not TimesFM. And everything else: the 20,000 batteries, where they sit and their physics, the 20 controllers and their crashes, decisions of the three "strategies", power, money, charge levels, the race, failed commands, availability, forecasts (not TimesFM), events, and the rest of the Results payload (cycles, top days, the other methods' city splits). None of the live numbers is a real result. |

The site shows a "Practice data" banner whenever it is connected to this mock (its `GET /health` version starts
with `mock`), the mock's forecasts are labelled `mock (simulated, not TimesFM)`, and its full-year results list
"SIMULATED practice data …" as their first assumption, saying which figures are copied and which are made up.

The years are 2025 (real prices all year) and 2026 (real prices through Sep 20, then the AI's prediction), as
`years` in `GET /settings`. The 2025 presets mirror `config.yaml` (`presets.items`; `ai_best_evening` is the
default, as there); each year also has "From January 1" (two days here), and 2026 has the evening its real prices end:

| Preset | Year | Replay starts (Central time) | Prices loaded |
|---|---|---|---|
| `spike_evening` | 2025 | May 16, 2025, 4 pm | real, May 16–17 |
| `morning_spike` | 2025 | Apr 7, 2025, 3 am | real, Apr 7–8 |
| `summer_spike` | 2025 | Jul 11, 2025, 6 pm | real, Jul 11–12 |
| `ai_best_evening` (default) | 2025 | May 30, 2025, 6 pm | real, May 30–31 |
| `ordinary_day` | 2025 | Jun 28, 2025, midnight | real, Jun 28–29 |
| `jan1_2025` | 2025 | Jan 1, 2025, midnight | real, Jan 1–2 |
| `predicted_2026` | 2026 | Sep 20, 2026, 6 pm | stand-in to midnight, then the simulated prediction for two days |
| `jan1_2026` | 2026 | Jan 1, 2026, midnight | stand-in, Jan 1–2 |

Settings take `year` too: a preset from another year answers 400 `invalid_preset`, a year other than 2025 or 2026
400 `invalid_year`, and a year alone picks that year's first preset (the default for 2025).

The price fixture is exported from the committed snapshot `data/snapshot/ercot_rt_2025_hubs.parquet` (ERCOT MIS
report 13061, see `data/snapshot/README.md`), reading the 2025 presets and the load zones (`fleet.locations`)
from `config.yaml` and adding the mock-only days above (`MOCK_ONLY`); presets of other years are skipped, since
the snapshot is 2025. To regenerate it after either changes, from the repo root after `make setup`:

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
- **Each controller's charge** (docs/API.md Worker): `charge_level` is the capacity-weighted charge of the
  batteries it runs now, `usable_charge` = (`charge_level` − 0.2) / 0.8, the share above the 20% reserve; both
  are `null` once it runs none (crashed and handed over, or restarting).
- **The failure model** (docs/API.md, the human's rules):
  - A crash (`POST /chaos/kill`) empties that controller's batteries to the reserve at once (`usable_charge` 0;
    the race shadow's same batteries too). The others take them over as before and run them, recharging them
    when it's cheap, so their own gauges dip. **Nothing comes back on its own**: the controller stays `dead`
    until Reset or a user restart.
  - `POST /workers/{worker_id}/restart` (public, one per 2 s like crashes): `200 {"worker_id", "status":
    "starting"}`, `409 not_crashed` unless it is `dead`, `404 unknown_worker`. At the next step (a second later
    while paused or finished) it takes its own block back with whatever charge those batteries have by then and
    emits `worker_recovered` ("Controller 7 is back and has taken back its 1,000 batteries in Houston, at 12%
    usable charge.", with `detail.worker_id`, `controller`, `batteries` and `by_location`).
  - Taking a controller's batteries offline (`/availability` scope `worker`) halves their usable charge at that
    moment (the race's too); while offline they don't move; back online they carry on from there. Going offline
    again while offline changes nothing. City and single-battery offline change no charge.
  - The potential fleet (all online) never loses charge, so `shortfall_usd` counts the lost energy.
- **Positions** (`GET /fleet/layout`, `layout.ts`): seeded Gaussian blobs around real places in each metro
  (downtowns, suburbs, satellite cities), kept inside the Texas outline, rounded to 0.001° like the API. The same
  fleet always gets the same map.
- **`battery_status`** in every snapshot and frame, as of the latest tick (like the backend): 0 offline, 1 waiting,
  2 selling, 3 charging (from each battery's setpoint), 4 for the two ticks its controller misses before the
  hand-over. So at 5-10 ticks a second a frame can miss code 4, as with the backend; the site then shows it from
  the worker events. As docs/API.md asks, a change shows within one frame even while paused: batteries taken
  offline are 0 at once, and a crashed controller's batteries are 4 from the moment it is killed until the
  hand-over.
- **Money per city**: `totals.by_location` holds revenue and energy per load zone; they add up to the totals.
- **The race** (`race` in every snapshot and frame while a run exists, `null` while idle): a shadow copy of the
  fleet on the comparison strategy, the simple rule (`bands`), or the AI forecast when the live run uses the simple
  rule. The same batteries and starting charge, the same prices at the same 15-minute decision lag, the same
  availability overrides; no controllers, so no crashes, no failed commands and no delivery noise.
  `live_minus_race_usd` is `totals.revenue_usd` minus the race's; `by_location` has each city's revenue, charge
  level and action. `potential` is a second shadow fleet, the live strategy with every battery online and no
  crashes ("All batteries online"), with `shortfall_usd` = its revenue minus the live fleet's, per city too;
  `shows_potential` turns true, for the rest of the run, once any battery goes offline or a controller is crashed.
- **`race_update` events**: when a spike (a price of $1,000/MWh or more) starts in any city, "Prices jumped to
  $1,760 in all four cities. The AI fleet had 90% charge left; the simple rule had 21%." (the charge levels as it
  began); when it ends, "The AI is now $55,800 ahead of the simple rule." (the leader first; `good` when the live
  strategy leads). On the default replay the simple rule leads until 9 pm, then the AI overtakes it in the spike.
- **The simulated "forecast" strategy** peeks at the loaded prices from now to 4 hours ahead, standing in for a
  good forecast, so it sells into a spike as it happens; the simple rule and the evening peak decide on the price
  published 15 minutes earlier.
- **2026's prediction**: ticks whose price is the prediction carry `predicted: true`, and the first one brings a
  `prediction_started` event (`detail.real_until_utc`) saying the prices from there on are the AI's prediction.

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
| `MOCK_COLD_START_MS` | Hold every request (and refuse WebSockets) for this long after start, like a Cloud Run cold start. Try `8000` to see "Waking up the simulator…". |
| `MOCK_WS=off` | Refuse WebSocket upgrades, so the site falls back to polling `GET /state`. |
| `MOCK_NO_RESULTS=1` | `GET /results/pitch` and `GET /results/ai-gains` answer 404 `no_results` until a full-year re-run finishes. |
| `MOCK_NO_AI_GAINS=1` | `GET /results/ai-gains` always answers 404 `no_results` (a backend without them yet): the Results page shows no "What the AI adds". |
| `MOCK_BACKTEST_FAIL=1` | Full-year re-runs end with status `failed`. |

## Behaviour worth knowing

- A replay runs until the end of its two loaded days, then reports status `finished` and a `run_finished` event.
- While no run exists, the WebSocket sends `{"type": "idle", "status": "idle", "run_id": null, "last_run": …}`
  every 5 s; `last_run` holds the id and totals of the run that was reset or replaced. Idle messages carry no
  `battery_status`, so after an availability change with no run the site fetches `GET /state` to redraw the map.
- `GET /fleet/layout` sends its version as an `ETag` and answers a matching `If-None-Match` with 304.
- Rate limits match docs/API.md: start and reset share one call per 5 s, availability 10 per second, crashes
  one per 2 s for everyone together, restarts likewise (their own limiter); limited calls answer 429 with
  `retry_after_s` and `Retry-After`.
- `POST /chaos/kill` is public (no passcode) and takes an optional `worker_id` (0–19; a random working one
  without it): 409 `already_dead` for a controller that is already down, 409 `too_few_healthy` when only two
  are left working, 404 `unknown_worker` for another number.
- A crashed controller misses two 5-minute steps: at the first it is "not responding", at the second it is marked
  crashed; its batteries (emptied at the crash) miss both steps' orders (those become failed commands when there
  was anything to do) and show as code 4, and are handed over after the second. Worker events carry `detail.worker_id`, `detail.controller` and, for `worker_suspect`
  and `worker_adopted`, `detail.by_location`; `worker_adopted` adds `adopted_by` and `ms`. While paused,
  detection uses wall-clock heartbeats instead (2 s, then 6 s), which is the easiest way to watch a crash on the
  map: pause, crash a controller, watch its batteries pulse red, then the hand-over.
- Cities that switch action at the same step share one event ("Batteries in all four cities started selling at
  about $1,757/MWh"), and so do price spikes.
- `POST /backtest` while a job runs returns that job's id; job status includes `skipped` (always empty here). A
  job takes 6 s.
- `POST /backtest/{job_id}/cancel` stops a running job: `200 {"status": "cancelled"}`, then its status reads
  `cancelled`, nothing is stored and the previous results stay; a `backtest_done` event says it was stopped.
  `409 not_running` once it has finished or been stopped, `404 unknown_job` for an unknown id.

## Files

| File | Purpose |
|---|---|
| `sim.ts` | The simulated run, the race, the years, the 2026 prediction and the failure model (pure; unit-tested in `sim.test.ts`, `v2.test.ts` and `failures.test.ts`, which also call the public crash and restart over HTTP) |
| `gains.ts` | `GET /results/ai-gains`: the measured 2025 numbers copied from RUNLOG.md |
| `layout.ts` | Simulated battery positions for `GET /fleet/layout` |
| `api.ts` | HTTP routes, errors, rate limits, WebSocket broadcast, CORS |
| `server.ts` | Standalone Node server (`npm run mock`) |
| `vitePlugin.ts` | Mounts the same API inside the Vite dev server |
| `export_prices.py` | Writes the real-price fixture from `data/snapshot` for every 2025 preset and load zone in `config.yaml`, plus the mock-only days |
| `data/` | The real-price fixture, with its source note inside |
