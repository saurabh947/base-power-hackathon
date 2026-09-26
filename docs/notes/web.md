# Notes: website (web/)

Owner: website builder. Everything lives in `web/`, plus `mk/web.mk` and this file. How to run and deploy it is
in `web/README.md`; the mock API is described in `web/mock/README.md`.

## What was built

- **Start** (`/`): headline and subhead from docs/UX.md; the run-settings card above the Start button; a
  "Change settings" editor for exactly preset, speed, decision method and starting charge; "Waking up the
  simulator… (usually a few seconds)" while `GET /health` is slow or failing; "A simulation is running. Watch
  live" plus "Start over" when `GET /health` reports a run.
- **Live** (`/live`): sticky top bar (replay clock in Houston time, "Replaying real prices from …", run and
  connection status, Pause/Resume, Speed, Take batteries offline, Reset with a confirmation, presenter-only
  Crash a controller); price chart with charge/sell shading and the forecast band; power asked for vs delivered
  with crash markers and the fleet charge level; 20 controller tiles with the failed-commands counter; the money
  counter; "What just happened" (last 6 `events[].message`); the availability drawer (per-area sliders, a
  controller's batteries, find a battery by ID, bring everything back).
- **Results** (`/results`): per-battery headline, fleet total, $/kWh-year, $/kW-year, cycles, per-area split;
  comparison bars including "Best possible (perfect hindsight)"; top 10 days; "How we got this number"
  expander (the four UX bullets plus "no peeking", then any extra `assumptions` from the API); "Re-run the full
  year" with progress (polls `GET /backtest/{job_id}` every 2 s, survives navigation via sessionStorage).
- **Presenter mode**: footer "Presenter" link → passcode dialog → `POST /presenter/check`; passcode kept in
  sessionStorage only and sent as `X-Presenter-Passcode`.
- **Language**: every panel has a one-line "What is this?" and an optional "How it works" (≤ 3 sentences);
  "Show technical details" reveals raw values and spec terms in brackets; numbers carry units and a human
  translation ("$120/MWh (12¢ per kWh)", "48.0 MW selling (about what 32,000 homes use)").

## Decisions

- **Stack**: React 19, Vite 8, TypeScript 6.0 (7.x is the Go port; kept to the version the ecosystem supports),
  uPlot for charts, oxlint for lint, vitest for tests. Runtime dependencies: react, react-dom, uplot. No router
  or state library: a 40-line History-API router and a 30-line external store cover three routes and one feed.
- **Why uPlot**: canvas, ~50 KB, comfortable with 2,000-point series updated every second, native bands (forecast
  p10–p90) and draw hooks (charge/sell shading, crash markers). The charts subscribe to the store directly and
  call `setData`, so a frame never re-renders React for them.
- **Re-renders**: verified in the browser with render counters: over live frames, `App`, `LiveScreen` and the
  price panel do not re-render; only panels whose slice changed (clock, money, controllers, feed) do.
- **Memory caps**: 2,000 ticks (same as `snapshot.history`), 50 events, 4 toasts.
- **Live connection**: opened while the Live screen is mounted, closed 30 s after leaving. Backoff 0.5 s → 15 s
  with ±25% jitter; a connection that lived ≥ 10 s and closes (Cloud Run's 60-minute limit) reconnects after
  ~0.5 s; quiet sockets (15 s without a message; idle messages come every 5 s) are closed by a watchdog and handshakes time out after 15 s
  (a slow Cloud Run cold start can therefore reach polling first, which is fine); after 3 failed WebSocket attempts it
  polls `GET /state` every 2 s and retries the WebSocket every 30 s. A frame with an unknown `run_id` triggers a
  `GET /state` resync.
- **Estimates used for translations** (always worded "about"): a typical Texas home averages about 1.5 kW
  (≈ 1,100 kWh a month), so 1 MW ≈ 670 homes and 36 kWh ≈ one home for a day.
- **Chart window**: 6 h (default), 24 h or whole replay, at least 6 h wide so a new run draws into empty space;
  it extends to the end of the latest forecast. The price chart shows one area at a time (Houston by default,
  since the demo spike is Houston's); both areas' current prices and actions are always listed above it.
- **Presets the backend cannot serve**: the site shows the server's error sentence (the mock answers 501 for a
  preset without loaded prices).
- **Mock**: dev server at `/__mock` (no second process), standalone on :8787; passcode `presenter` (mock only).
  Real prices for all four presets in `config.yaml` (two days each). `scripts/check-dist.mjs` fails the build if
  mock markers appear in `dist/`.
- **Production builds** have no source maps.

## How the site follows the pinned contract (docs/API.md, commit 0a2396f)

- **Controllers**: `worker_id` 0–19 is shown as "Controller N" with N = worker_id + 1 in tiles, the crash picker,
  toasts, the availability drawer and the battery card (`src/lib/fleet.ts: controllerName`). Event messages are
  shown verbatim. Crash markers and the "Latest handover" line read `detail.controller` (falling back to
  `detail.worker_id + 1`), `detail.adopted_by` and `detail.ms`.
- **Battery ids**: fleet-wide `b00000`–`b19999`. "Find a battery" takes a number (0–19,999) or an id (`b123`,
  `b00123`), rejects anything outside the fleet, and says which area it is in; the area ranges come from
  `settings.fleet_size` and `settings.locations` with the same split as `src/sim/fleet.location_counts` (tested
  against the Python on the same inputs).
- **`GET /settings`**: reads Preset `{id, label, date_local, start_utc, start_local, description}`, Strategy
  `{id, label, description}` and Speed `{tps, label}`. Malformed items are skipped and a failed request falls back
  to the documented defaults. Speed options are worded by the site ("1 hour of market time every 6 seconds").
- **WebSocket**: the idle message `{"type": "idle", "status": "idle", "run_id": null, "last_run": …}` clears the
  run view (after a reset the old tiles and offline counts are stale), and `last_run` feeds "The last replay
  earned $X" on the Live screen. The quiet-socket watchdog is 15 s, since idle messages come every 5 s.
  `sim_time_utc` and `speed_tps` are read from the snapshot.
- **`run_finished`**: status `finished` shows "Replay finished" with the final gross wholesale value, energy sold
  and bought, and buttons to start a new replay or see full-year results.
- **404s**: `no_results` shows "No full-year results yet." with the Re-run button (the server sentence is not
  repeated). The site does not call `GET /forecast`; forecasts arrive on the WebSocket, and with none there is no
  band. `GET /backtest/{id}` 404 (a job the server forgot) quietly clears the saved job.
- **Backtest**: `skipped` reasons are shown after a re-run ("AI forecast (TimesFM-3) was left out: …").
- **Amounts**: `top_days[].usd`, `per_location_usd` and `revenue_usd` are fleet totals; `error_pct` is a
  percentage ("On target" means ≤ 5).

## Requests for integrator

1. **docs/API.md leftover**: the `POST /availability` battery example still says
   `"battery_id": "LZ_HOUSTON-000123"`; it should read `"b00123"`.
2. **Vercel**: Root Directory `web`; set `VITE_API_BASE_URL` to the Cloud Run URL for Production (and Preview if
   used); a Vercel build without it fails on purpose. `.vercelignore` in `web/` keeps `.env*` out of uploads.
3. **After `POST /run/speed`**, `snapshot.speed_tps` and `settings.speed_tps` should both show the new speed (the
   site reads `speed_tps` first).
4. **Forecast label**: the site calls the band "AI forecast, likely range" and names TimesFM-3 only in the
   decision-method label from `GET /settings`/results; if the model switch moves to TimesFM 2.5, send the new
   label there and the site follows.
