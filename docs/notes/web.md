# Notes: website (web/)

Owner: website builder. Everything lives in `web/`, plus `mk/web.mk` and this file. How to run and deploy it is
in `web/README.md`; the mock API is described in `web/mock/README.md`.

## What was built

- **Start** (`/`): headline and subhead from docs/UX.md; the run-settings card above the Start button (Where:
  "Dallas–Fort Worth, Houston, San Antonio, and Austin", the Texas Triangle); a "Change settings" editor for exactly
  preset, speed, decision method and starting charge; "Waking up the simulator… (usually a few seconds)" while
  `GET /health` is slow or failing; "A simulation is running. Watch live" plus "Start over" when `GET /health`
  reports a run.
- **Live** (`/live`) opens on the **live map** (docs/UX.md, "Live map"):
  - a full-window, always-dark gray page (#25282c) with Texas as a bright red outline and the 20,000 batteries as
    green dots from `GET /fleet/layout` and `battery_status`;
  - clickable Texas Triangle cities: label buttons beside each cluster, or click a cluster; the "All of Texas" chip,
    Esc or empty map go back; the selected city's dots stay bright and the others dim a little;
  - four compact translucent corners: top-left the replay clock and the price (all four cities, or the selected
    city's price with its ¢/kWh, a 6-hour mini line with charge/sell shading and the forecast band); top-right
    money earned (fleet or `totals.by_location`) and power asked for vs delivered (numbers, sparkline, On target);
    bottom-left Pause/Resume, Speed, Reset, Take batteries offline, the presenter-only Crash a controller, "Show
    charts" and the Presenter link; bottom-right the 20 controllers (those serving the selected city highlighted,
    from `workers[].by_location`), batteries online and by state (doubling as the dot legend), and the last 3
    events;
  - the caption "Battery locations are simulated inside real metro areas.";
  - "Show charts" switches to the detailed layout from before (top bar, price chart with a 4-city toggle that
    follows the map's selection, power chart, controllers, money, events, the availability drawer); "Show map"
    switches back. The choice is kept for the tab.
- **Results** (`/results`): per-battery headline, fleet total, $/kWh-year, $/kW-year, cycles, per-area split;
  comparison bars including "Best possible (perfect hindsight)"; top 10 days; "How we got this number"; "Re-run the
  full year" with progress.
- **Presenter mode**: "Presenter" link (footer, or the map's controls corner) → passcode dialog →
  `POST /presenter/check`; passcode kept in sessionStorage only and sent as `X-Presenter-Passcode`.
- **Language**: every panel has a one-line "What is this?" and an optional "How it works"; "Show technical details"
  reveals raw values and spec terms in brackets (on the map too: location ids, raw kW, error_pct, status codes,
  and the map's own sizes and timings); numbers carry units and a human translation.

## Decisions

- **Stack**: React 19, Vite 8, TypeScript 6.0, uPlot for the detailed charts, plain canvas and SVG for the map,
  oxlint, vitest. Runtime dependencies: react, react-dom, uplot. No map library: the projection, the outline and
  the aggregation are a few hundred lines of tested TypeScript.
- **Projection**: `src/map/albers.ts` is the ellipsoidal Albers equal-area conic (Snyder 1987) with the EPSG:3083
  parameters (GRS 1980, 27.5°N/35°N, origin 18°N 100°W, false easting 1,500,000 m, northing 6,000,000 m). It matches
  PROJ's `aea` reference values to 0.1 mm, maps the false origin exactly, is true to scale on both standard
  parallels and keeps area (tested). The outline generator and the dots use the same function.
- **Outline**: U.S. Census Bureau 2023 cartographic boundary file, states, 1:5,000,000 (public domain, no access
  constraints). The Texas ring (3,791 points; the 5m file has no separate islands) is projected, simplified with
  Douglas–Peucker at 2 km to **351 points** (7.7 KB), checked for self-intersections, rounded to 10 m; its area is
  688,924 km² (Census land + water 695,668 km²). Tests put Houston, Dallas, Fort Worth, Austin, San Antonio,
  Amarillo and Brownsville inside, El Paso inside the westernmost 3%, and Oklahoma City, Albuquerque, Shreveport,
  Lake Charles, Monterrey, Chihuahua and the Gulf outside. `make web-texas` rebuilds it.
- **Fit**: Texas fills the window with a small margin (more below it for the caption). Spare width goes 38/62 to
  the left/right, because the right-hand panels sit beside the Louisiana border and the Gulf coast while the
  left-hand ones sit over New Mexico and Mexico. The "All of Texas" chip sits over Oklahoma, right of the panhandle.
- **Dots**: batteries are binned into a hexagonal grid in screen pixels, about 140 cells from top to bottom (4–9 px),
  so the count is the same at any size: **448 dots** for the mock fleet. A dot's area is proportional to its
  batteries (the 95th-percentile cell nearly fills its hex; below 0.9 px a dot keeps the radius and fades, so its
  ink still matches its count). Each dot sits halfway between its batteries' centroid and its hex centre.
- **Dot looks**: a dot mixes its batteries' looks by count: waiting medium green, selling bright green with a soft
  additive glow, charging green with a thin light ring, offline dim gray (30% offline is 30% of the way to gray).
  All four are ≥ 3:1 against the page (tested; the offline gray was lightened for that), selling brighter than
  waiting. The legend in the bottom-right corner uses the same colours (tested against the canvas palette).
- **The crash flash**: the backend gives each controller a contiguous block of 1,000 batteries (one city, or two
  for controller 18), spread over that city. Each dot gets a dark core whose area is the share of its batteries that
  are "controller down", pulsing; when they are handed over the cores close and those dots light up green in
  proportion. A full-dot darkening or per-battery specks would black out the whole city for a 12% event (measured:
  brightness in the Dallas–Fort Worth cluster fell from 35 to 8 with specks).
- **When a frame misses code 4**: code 4 lasts about two ticks, so above 2 ticks a second a 1 s frame can miss it.
  The worker events cannot be missed: on `worker_suspect`, `worker_dead` or `worker_adopted` with no code 4 in the
  frame (and none shown in the last 2.5 s), that controller's batteries are shown as down for that frame, and the
  next frame brings them back with the usual re-light. Which batteries a controller runs is replayed on the client
  (`src/map/owners.ts`): the default blocks (numpy `array_split`) and each hand-over's `adopted_by`, round-robin in
  id order as `coordinator._reassign` does. Events older than 60 s (a reconnect replaying history) don't flash.
- **Drawing**: two canvases, the outline (glow, redrawn on resize only) and the dots (redrawn on every frame).
  Between frames nothing is drawn unless a transition (160 ms), a flash or a re-light (900 ms) plays; then a
  `requestAnimationFrame` loop runs until everything settles, and stops. `prefers-reduced-motion` turns off glows,
  pulses and transitions.
- **Data flow**: `battery_status` is kept as the raw string in the live state; `src/map/feed.ts` decodes it once
  per frame (with the layout's city blocks) into codes and per-city counts shared by the canvas and the corners.
  The layout is fetched once while a map is on screen and revalidated with its ETag; a fleet-size change refetches
  it. With no `battery_status` at all (an older backend) each city's dots show its current action.
- **Always dark**: while the map is shown, `<html data-theme="map">` redefines every color token with neutral grays
  (so the header, dialogs, toasts and drawer match) and the footer is hidden; its Presenter link moves into the
  controls corner. The theme is AA-checked like the light and dark ones. Toasts sit bottom-centre on the map.
- **Labels**: each city's label goes on the side that suits the geography (north of Dallas–Fort Worth, east of
  Houston, west of Austin, south-west of San Antonio) unless that would cover a corner panel, the chip, a notice,
  another label or another city's dots; placement re-runs when a panel changes size.
- **Narrow windows** (≤ 860 px): the map comes first and the corners stack below it.
- **Language details**: times say "Central time" (all four cities keep America/Chicago time), like the backend's
  event messages. City names come from `GET /fleet/layout` `cities[].name`, falling back to the documented four.
- **Re-renders**: the map stage does not re-render per frame; the canvas is fed outside React, and each corner
  reads its own slices.
- **Mock** (`web/mock`): real prices for all four load zones and all four presets (Houston/DFW values identical to
  the previous export); simulated metro positions (seeded Gaussian blobs around real suburbs, kept on land);
  contiguous controller blocks and round-robin hand-over like the backend; `battery_status` as of the latest tick,
  4 for the two missed ticks (so it misses code 4 above 2 ticks a second, like the backend); `totals.by_location`,
  `workers[].by_location`, `by_location` in worker events; `GET /fleet/layout` with an ETag. Never in `dist/`
  (`scripts/check-dist.mjs`).

## Measurements (mock, Chrome, 1280×800 and 1920×1080)

- **Frame**: 18.7 KB of JSON at 1 tick per frame, of which `battery_status` is 13,336 characters; about 1 KB
  deflated (a uniform `battery_status` deflates to 37 bytes). The snapshot on connect was 143 KB with 169 ticks of
  history. `GET /fleet/layout`: 106,668 characters of positions.
- **Per frame**: decode `battery_status` + re-count per cell + restyle 0.4–0.5 ms; paint 0.1–0.2 ms (slowest of the
  last 120 paints 0.4–0.6 ms, during transitions); building the cells after a resize or a new layout 2.5–4.7 ms.
  448 dots at both sizes.
- **Bundle**: 424.2 KB JS (141.0 KB gzip) and 37.2 KB CSS, up from 372.4 KB JS: the map code and the 7.7 KB outline.

## How the site follows the contract (docs/API.md)

- **Controllers**: `worker_id` 0–19 is shown as "Controller N" with N = worker_id + 1 everywhere. Event messages
  are shown verbatim. Crash markers and the "Latest handover" line read `detail.controller`, `detail.adopted_by`
  and `detail.ms`; the map's flash also reads `detail.worker_id`.
- **Battery ids**: fleet-wide `b00000`–`b19999` in the four city blocks; "Find a battery" names the block's city
  and, with technical details, shows `lonlat`. The ranges come from `settings.fleet_size` and `settings.locations`
  with the same split as `src/sim/fleet.location_counts` (tested for the 4-city shares and the old 50/50).
- **`GET /fleet/layout`**: read defensively (Int16 little-endian positions, lon = −100 + v/1000,
  lat = 31 + v/1000); cities in id order; missing names fall back to the documented ones.
- **`battery_status`**: 4-bit codes, battery i in byte i >> 1, low nibble for even i (tested); codes above 4 read as
  waiting; a short or malformed field falls back to "all waiting". Offline shows at once when idle: the site
  fetches `GET /state` after its own availability change, since idle messages carry no `battery_status`.
- **`totals.by_location`, `availability.by_location`, `workers[].by_location`**: missing cities read as 0 (a dead
  controller has `{}`); a city's "On target" uses the tick's `error_pct` formula with its own target and actual and
  0.5% of its estimated nameplate.
- **`POST /run/start` 503 `simulator_restarting`**: "The simulator is still restarting after a reset. Its 20
  controllers are coming back up. Try again in N seconds.", with the Start button counting down `retry_after_s`.
  The Start request waits up to 35 s, since the server holds it up to 15 s after a Reset.
- **Everything else** as before: settings item shapes, the idle message and `last_run`, `run_finished`, 404
  `no_results`, `skipped` reasons, amounts as fleet totals, `error_pct` as a percentage.

## Open issues

- The event-triggered flash relies on the client's replay of ownership. If a viewer joins after more than 50
  events (the kept history), earlier hand-overs are unknown and a later crash of an adopter flashes only its
  original block. It never flashes the wrong city for a first crash.
- The map was checked against the mock only; it was not run against the merged backend here.
- `prefers-reduced-motion` could not be emulated in the browser pane; that path is covered by unit tests (no
  glows) and by reading the renderer (no loop, no pulses).

## Requests for integrator

1. **docs/API.md, `battery_status` and code 4**: say that code 4 lasts about two ticks and a frame can miss it
   above 2 ticks a second. The site now flashes from the worker events (and replays ownership from
   `adopted_by`); a simpler contract would be for the API to report 4 for any battery that was down at a tick since
   the previous frame (the mock did this before the merge), or to add the down batteries' ids to `worker_adopted`.
2. **docs/API.md, other additions from the merge**: the `503 simulator_restarting` code with `retry_after_s` and
   `Retry-After`; `detail.by_location` on `worker_suspect` and `worker_adopted`; `GET /fleet/layout` served with an
   `ETag` (304 on `If-None-Match`); `workers[].by_location` lists only the cities a controller has (`{}` when
   dead); idle messages carry no `battery_status`.
3. **docs/UX.md** still speaks of two areas and "Houston time": the Start card ("Houston & Dallas–Fort Worth"), the
   glossary, principle 5 and the Live top bar. The site says "Central time" (as the backend's events do) and lists
   all four cities; please update the doc to match.
4. **WebSocket compression**: a frame is ~18.7 KB raw but ~1 KB deflated (`battery_status` is most of it and
   compresses to almost nothing). Please make sure the Cloud Run WebSocket negotiates permessage-deflate
   (uvicorn's `ws_per_message_deflate`, on by default with the `websockets` implementation); the snapshot on
   connect (up to ~1.8 MB with four cities) benefits most.
5. **Vercel**: Root Directory `web`; set `VITE_API_BASE_URL` to the Cloud Run URL for Production (and Preview if
   used); a Vercel build without it fails on purpose. `.vercelignore` in `web/` keeps `.env*` out of uploads.
6. **After `POST /run/speed`**, `snapshot.speed_tps` and `settings.speed_tps` should both show the new speed (the
   site reads `speed_tps` first).
7. **Forecast label**: the site calls the band "AI forecast, likely range" and names TimesFM-3 only in the
   decision-method label from `GET /settings`/results; if the model switch moves to TimesFM 2.5, send the new label
   there and the site follows.
