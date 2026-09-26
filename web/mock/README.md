# Mock API (development and tests only)

A stand-in for the fleet-sim backend so the website can be built and tested before, and without, the real
one. It implements the REST endpoints and the WebSocket `/live` messages from [`docs/API.md`](../../docs/API.md).

## What is real and what is simulated

| | |
|---|---|
| **Real** | Electricity prices: ERCOT real-time settlement point prices for load zones LZ_HOUSTON and LZ_NORTH, 15-minute intervals, Chicago-local dates **May 16 and 17, 2025** (`data/ercot_rt_lz_2025-05-16_17.json`, 192 rows per area). |
| **Simulated** | Everything else: the 20,000 batteries and their physics, the 20 controllers and their crashes, decisions of the three "strategies", power, money, charge levels, failed commands, availability, forecasts (not TimesFM), events, and every number on the Results screen. None of it is a real result. |

The site shows a "Practice data" banner whenever it is connected to this mock (its `GET /health` version starts
with `mock`), the mock's forecasts are labelled `mock (simulated, not TimesFM)`, and its full-year results list
"SIMULATED" as their first assumption.

The price fixture was exported from the committed snapshot `data/snapshot/ercot_rt_2025_hubs.parquet` (ERCOT MIS
report 13061, see `data/snapshot/README.md`). To regenerate it, from the repo root after `make setup`:

```sh
uv run python web/mock/export_prices.py      # or: make web-prices
```

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
| `MOCK_NO_RESULTS=1` | `GET /results/pitch` answers 404 until a full-year re-run finishes. |
| `MOCK_BACKTEST_FAIL=1` | Full-year re-runs end with status `failed`. |

## Behaviour worth knowing

- Only the "Price-spike evening" replay has prices here; the other presets answer **501** with a plain sentence
  (a handy way to see the site's 501 handling).
- The replay starts at 4 pm on May 16 and stops at midnight after May 17 (status `finished`).
- Rate limits match docs/API.md: start and reset share one call per 5 s, availability 10 per second, wrong
  passcodes 5 per minute; limited calls answer 429 with `retry_after_s` and `Retry-After`.
- A crashed controller misses one 5-minute step ("not responding": its batteries miss their orders, and those
  orders become failed commands), then is marked crashed and its batteries are handed round-robin to the healthy
  ones. While paused, detection uses wall-clock heartbeats instead (2 s, then 6 s).
- Worker ids are 1–20 and battery ids are `LZ_HOUSTON-000000` … `LZ_HOUSTON-009999` and `LZ_NORTH-000000` …
  `LZ_NORTH-009999`. The site does not rely on either choice.

## Files

| File | Purpose |
|---|---|
| `sim.ts` | The simulated run (pure; unit-tested in `sim.test.ts`) |
| `api.ts` | HTTP routes, errors, rate limits, WebSocket broadcast, CORS |
| `server.ts` | Standalone Node server (`npm run mock`) |
| `vitePlugin.ts` | Mounts the same API inside the Vite dev server |
| `export_prices.py` | Writes the real-price fixture from `data/snapshot` |
| `data/` | The real-price fixture, with its source note inside |
