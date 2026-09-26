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
  ~0.5 s; quiet sockets (10 s without a message) are closed by a watchdog and handshakes time out after 15 s
  (a slow Cloud Run cold start can therefore reach polling first, which is fine); after 3 failed WebSocket attempts it
  polls `GET /state` every 2 s and retries the WebSocket every 30 s. A frame with an unknown `run_id` triggers a
  `GET /state` resync.
- **Estimates used for translations** (always worded "about"): a typical Texas home averages about 1.5 kW
  (≈ 1,100 kWh a month), so 1 MW ≈ 670 homes and 36 kWh ≈ one home for a day.
- **Chart window**: 6 h (default), 24 h or whole replay, at least 6 h wide so a new run draws into empty space;
  it extends to the end of the latest forecast. The price chart shows one area at a time (Houston by default,
  since the demo spike is Houston's); both areas' current prices and actions are always listed above it.
- **Presets the backend cannot serve**: the site shows the server's error sentence (the mock uses 501 for
  presets outside its May 16–17 fixture).
- **Mock**: dev server at `/__mock` (no second process), standalone on :8787; passcode `presenter` (mock only).
  `scripts/check-dist.mjs` fails the build if mock markers appear in `dist/`.

## Assumptions about docs/API.md (the site tolerates both readings unless noted)

- `GET /state` returns the snapshot object; `"type": "snapshot"` is added if missing.
- `error_pct` is already a percentage (0.02 means 0.02%, as in the example); "On target" means ≤ 5.
- `top_days[].usd` is the whole fleet's revenue that day; the site also shows it divided by `fleet_size`.
- `per_location_usd` is the fleet's yearly revenue per area.
- The site shows "Controller {worker_id}" verbatim, to match `events[].message`; it assumes nothing about the id
  range.
- Crash markers on the power chart come from `worker_dead` events and read `detail.worker_id` when present.
- A dead worker reports `batteries: 0` after adoption; the tile then says "Handed to others".

## Requests for integrator

1. **Pin the `Choice` shape** in `GET /settings` (`strategies`, `speeds`), e.g. `{"value": 2, "label": "…",
   "description": "…"}`. The site accepts bare values, `{value}`, `{id}`, `{speed_tps}` or `{strategy}` for now.
2. **Pin the `idle` WebSocket message** body. The site accepts `{"type": "idle"}` with optional `status`,
   `events`, `availability` and `workers`, and also treats a `frame` whose status is `idle` as idle.
3. **CORS must allow the request headers `Content-Type` and `X-Presenter-Passcode`** (JSON bodies and presenter
   calls are preflighted) and should expose `Retry-After`.
4. **429 bodies**: please include `message` as well as `retry_after_s` (API.md says every error has one; its
   429 example does not). The site words 429 itself either way.
5. **Battery ids**: API.md says `LZ_HOUSTON-000123` but `src/sim/fleet.py` makes `b00000`. Please confirm the
   public format and whether the number is per area (000000–009999) or fleet-wide. The "Find a battery" form
   builds `{area}-{6 digits}` from a number and also accepts a full ID.
6. **Worker ids**: confirm 0–19 or 1–20, and that `events[].message` says "Controller N" with the same N.
7. **No-results answer**: what does `GET /results/pitch` return before any backtest? The site shows "No
   full-year results yet" plus the Re-run button for 404 and 501, and for an empty `strategies` list.
8. **End of a replay**: consider an event kind `run_finished` (the list has none); the site handles
   `status: "finished"` with a "This replay has finished" notice.
9. **Snapshot fields**: it would help if `snapshot` carried `sim_time_utc` and the current `speed_tps` (the site
   falls back to the last tick and `settings.speed_tps`; after `POST /run/speed`, `settings.speed_tps` should
   reflect the new speed).
10. **`worker_dead` events**: include `detail.worker_id` (number) so crash markers can be labelled.
11. **Vercel**: Root Directory `web`; set `VITE_API_BASE_URL` to the Cloud Run URL for Production (and Preview if
    used); a Vercel build without it fails on purpose. `.vercelignore` in `web/` keeps `.env*` out of uploads.
12. **Forecast label**: the site calls the band "AI forecast, likely range" and names TimesFM-3 only in the
    decision-method label from `GET /settings`/results; if the model switch moves to TimesFM 2.5, send the new
    label there and the site follows.
