# fleet-sim website

The public site for fleet-sim: replay real Texas (ERCOT) electricity prices (2025, or 2026 with the AI's prediction
after its real prices end) and watch 20,000 simulated home batteries buy cheap, sell high, and keep working when
controllers crash. React + Vite + TypeScript,
deployed on Vercel; the backend runs on Google Cloud Run and is described in [`docs/API.md`](../docs/API.md).
Screens and wording follow [`docs/UX.md`](../docs/UX.md).

## Run it locally

Node 22.12 or newer.

```sh
cd web
npm i
npm run dev          # http://localhost:5173, with the built-in mock API
```

With `VITE_API_BASE_URL` unset, `npm run dev` serves a **mock API** at `/__mock` on the dev server itself, so the
whole site works offline. The mock replays **real** ERCOT prices for the four Texas Triangle load zones for each
2025 replay preset (two days each); its 2026 is 2025's real prices a year on, then a simulated prediction;
everything else it serves, including where the batteries sit, is **simulated**, and
the site shows a "Practice data" banner while connected to it. See [`mock/README.md`](mock/README.md).

Against a backend running on your machine (`uvicorn`, port 8000):

```sh
npm run dev:local    # same as VITE_API_BASE_URL=http://localhost:8000 npm run dev
```

Against any backend, including Cloud Run:

```sh
VITE_API_BASE_URL=https://fleet-sim-XXXXXXXX.us-east1.run.app npm run dev
```

The standalone mock, for trying a production build or pointing another client at it:

```sh
npm run mock                                        # http://localhost:8787
VITE_API_BASE_URL=http://localhost:8787 npm run build && npm run preview   # http://localhost:4173
```

From the repo root the same things are `make web-install`, `make web-dev`, `make web-dev-local`, `make web-mock`
and `make web-check` (see `mk/web.mk`). `make web-prices` re-exports the mock's real prices and `make web-texas`
rebuilds the Texas outline (below).

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server; mock API at `/__mock` unless `VITE_API_BASE_URL` is set |
| `npm run dev:local` | Dev server against `http://localhost:8000` |
| `npm run mock` | Standalone mock API on port 8787 (`MOCK_PORT` to change) |
| `npm run typecheck` | `tsc -b` over the app, the mock and the configs |
| `npm run lint` | oxlint (correctness, suspicious, React hooks, jsx-a11y), warnings fail |
| `npm test` | vitest: formatters, frame reducer, reconnect logic, settings (years and their presets), chart model, results rows, "What the AI adds" (uplift maths, wording, the pitch year only), the full-year re-run's Stop, the race (with the all-online third bar) and its events, map (projection, outline, layout and status decoding, cells, dot looks, zoom viewports, selection, labels), the timeline (past state, frame recorder, track), crash errors, mock (2026 prediction, race potential, status while paused, public kill over HTTP), AA contrast of the site's one theme |
| `npm run build` | typecheck, `vite build` to `dist/` (no source maps), then `scripts/check-dist.mjs` fails if any mock code shipped |
| `npm run check` | all of the above |

## Configuration

One setting, read at **build time**:

| Variable | Meaning |
|---|---|
| `VITE_API_BASE_URL` | Backend base URL, e.g. the Cloud Run URL. The WebSocket URL is derived from it (`https` → `wss`, plus `/live`). Not a secret. |

- Unset in `npm run dev`: the mock API is used.
- Unset in a production build: the site shows "This site is not connected to a simulator".
- Unset on Vercel (`VERCEL=1`): the build fails on purpose with a message saying what to set.

Every control is public: there is no presenter mode and no passcode ("Crash a controller" is open to everyone,
one crash every 2 s). Requests carry no credentials, only `Content-Type`. The site has no footer.

## Deploy to Vercel

The human owns the Vercel account; the integrator deploys. The project's **Root Directory is `web/`**.

1. Log in once: `npx vercel login`.
2. Link the project from this folder: `cd web && npx vercel link` (create a new project, e.g. `fleet-sim`).
   With Git integration instead, set *Settings → General → Root Directory* to `web`. `vercel.json` sets the
   framework, `npm ci`, `npm run build` and `dist/`.
3. Set the backend URL for Production (and Preview if wanted):
   `npx vercel env add VITE_API_BASE_URL production`, then paste the Cloud Run URL
   (`gcloud run services describe fleet-sim --region us-east1 --format='value(status.url)'`).
4. Deploy: `npx vercel --prod`.
5. CORS: the API allows `https://*.vercel.app` (docs/API.md). For a custom domain, pass it to
   `scripts/cloud/deploy.sh https://your.domain` so it lands in `CORS_ORIGINS`.
6. Check: open the site, turn on "Show technical details" and look at the header: it shows the API URL and the
   backend version. The Live screen should say "Live updates".

`vercel.json` also rewrites every path to `index.html` (the site uses `/`, `/live` and `/results`), caches the
hashed files in `/assets` for a year, and sets a few security headers. Vercel runs Node 22 per `engines`.

## How the site is built

```
src/
  api/          types.ts (the docs/API.md contract), client.ts (fetch + ApiError), errors.ts (friendly
                messages for 429/501/503/…), settings.ts (tolerant GET /settings reader), config.ts (base URL)
  live/         connection.ts (WebSocket + backoff + watchdog + GET /state polling fallback),
                reducer.ts (snapshot/frame/idle → view, capped, structurally shared), liveStore.ts,
                the timeline: recorder.ts (the frames this page saw), past.ts (the view at an earlier
                moment), timeTravel.ts (which moment is shown)
  map/          the live map's engine: albers.ts (EPSG:3083), texasOutline.ts (generated), geometry.ts,
                layout.ts (GET /fleet/layout), status.ts (battery_status nibbles), cells.ts (screen-space
                aggregation), looks.ts (dot looks), viewports.ts (the one zoom level), renderer.ts (canvas),
                selection.ts, owners.ts and flash.ts (the crash flash from events), feed.ts
  lib/          format.ts (units + plain-language translations), cities.ts (names from the layout), fleet.ts,
                store.ts, router.ts, contrast.ts
  state/        backend health / wake-up, technical-details switch, toasts
  components/   Panel ("What is this?" + "How it works"), Tech/Term, Dialog, charts/ (uPlot)
  screens/      start/, live/ (map/ is the default view, the charts layout is behind "Show charts"), results/
  styles/       tokens.css (the one always-dark theme, AA-checked), app.css, map.css
scripts/        check-dist.mjs (no mock in dist/), texas-outline.ts (builds src/map/texasOutline.ts)
mock/           simulated API for dev and tests only (never in dist/)
```

### The live map

The Live screen opens on a full-window map (docs/UX.md, "Live map" and "Live map v2"): a dark gray page, Texas
as a bright red outline, and the 20,000 batteries as dots coloured by what they are doing, with the four corners
holding the timeline, clock and price, money and power, the controls, and the controllers, batteries and latest
events. "Show charts" switches to the detailed layout (remembered for the tab); "Show map" switches back.

- **Projection.** `src/map/albers.ts` implements the ellipsoidal Albers equal-area conic (Snyder 1987) with the
  EPSG:3083 parameters; it matches PROJ's reference values. The outline and the dots use the same function.
- **Outline.** `src/map/texasOutline.ts` is generated by `scripts/texas-outline.ts` from the U.S. Census Bureau
  2023 cartographic boundary file (states, 1:5,000,000, public domain): the Texas ring, projected, simplified with
  Douglas–Peucker at 2 km to 351 points, and checked to be a simple polygon. `make web-texas` downloads the file
  to a temp dir and rebuilds it.
- **Dots.** Batteries are binned into a hexagonal grid in screen pixels (about 140 cells top to bottom, so a few
  hundred dots at any size); each dot's area is proportional to its battery count and it sits between its
  batteries' centroid and its hex centre. A dot takes the look of the MAJORITY of its batteries, with one
  meaning per look and shape as well as colour (`src/map/looks.ts`, tested): green solid with a soft halo when
  selling, blue solid when charging, small gray when waiting, a hollow red ring when offline, solid red and
  pulsing when their controller is down. A dot with ANY offline or crashed batteries also gets a red edge, wider
  and brighter the larger their share, so an outage never hides inside a green or blue dot. The counts in the
  bottom-right corner are the legend, each next to the same dot.
- **Controller down (code 4).** Those batteries' dots turn solid red and pulse, where the batteries are, until
  another controller takes them over; then they go back to their normal look and briefly show a ring. Code 4
  lasts about two ticks, so at 5-10 ticks a second a frame can miss it: then the
  `worker_suspect`/`worker_dead`/`worker_adopted` events trigger the same look on that controller's batteries,
  which `src/map/owners.ts` replays from the default blocks and each hand-over's `adopted_by`.
- **Crashing a controller** is public: "Crash a controller" in the controls corner crashes a random one, and
  clicking a controller tile asks "Crash controller 7?" in a small popover first. One crash every 2 s for
  everyone: the button counts down ("Next crash in 2 s"), a 429 says "One crash every 2 seconds, please.", and
  the 409s get plain sentences ("That controller has already crashed.", "At least 2 controllers must keep
  working."); see `src/screens/live/crash.ts` and `src/api/errors.ts` (tested).
- **Zoom, one level** (`src/map/viewports.ts`, tested). The map loads on all of Texas; "+", a double-click or
  the "+" key zooms to the Texas Triangle (the box around the four metros' batteries with a margin, centred
  between the corner panels); "−", Esc or the "−" key zooms out. The zoom is a 650 ms scaling about the one
  screen point both views share, crossfading the dots as they re-aggregate for the new scale; city labels move
  with it. `prefers-reduced-motion` makes it instant.
- **Timeline** (top-left corner, `Timeline.tsx` + `timeline.ts`, tested). The track is a small price line for
  the run so far, green where the fleet sold, blue where it bought, ▲/▼ at each day's high and low, and the AI's
  prediction shaded. Dragging it, or ←/→ (Shift for an hour, Home/End), shows the map and every corner as they
  were (a "Viewing 7:45 pm · Back to live" chip; End on the timeline also returns); the simulation keeps running. The page
  records each frame's `battery_status` (packed, 8 MB cap, thinning the older half beyond it,
  `src/live/recorder.ts`); before the page was opened the corners rebuild the moment from the tick history
  (`src/live/past.ts`) and the map shows city totals only, which a one-line hint says.
- **2026.** On ticks with `predicted: true` the clock gets a "Predicted" badge, the money is labelled
  "projected", the timeline and price chart shade the predicted part, and `prediction_started` shows in the
  event feed.
- **Drawing.** Two canvases: the outline (redrawn on resize and zoom) and the dots (redrawn on each 1 Hz
  frame). `requestAnimationFrame` runs only while a transition, a pulse, a zoom or a re-light plays.
  `prefers-reduced-motion` turns off halos, pulses and transitions.
- **Cities.** Labels are real buttons placed beside each cluster (avoiding the corner panels); clicking a city's
  dots also selects it; the "All of Texas" chip, Esc or clicking empty map go back. The corners then show that
  city: per-location tick values, `totals.by_location`, `availability.by_location`, and the controllers with
  batteries there (`workers[].by_location`) highlighted.
- **The race.** The money corner shows the live fleet against a copy of the same batteries on the simple rule
  (`race` in every frame, docs/API.md): "RACE ↑ AI forecast ahead by $15,290" over two bars, for all of Texas or
  the selected city (`race.by_location`), hidden while idle. When `race.shows_potential` is true (batteries have
  been offline or a controller down this run) a third bar shows `race.potential`, the same fleet with every
  battery online, and the shortfall: "All online … projected · Outages cost $790 so far" in the corner, the full
  label in the money card. "Show charts" has the same in its money card, with the charge left on each side;
  `race_update` events get a flag in both "What just happened" lists. The wording is in
  `src/screens/live/race.ts` (tested). In a short window the money corner is capped at the room the fleet corner
  below it leaves, so the two never overlap.
- **Batteries offline.** "Take batteries offline" opens a drawer on the LEFT with a light backdrop, so the cities
  stay visible while their dots turn red; "Bring every battery back online" is at its top.
- **Always dark, everywhere.** The map's look is the whole site's: one set of tokens (`src/styles/tokens.css`),
  always dark whatever the browser's colour preference, with the same translucent panels on every page, green for
  selling and the primary action, blue for charging, violet for the AI's forecast and prediction, and red for
  outages, failures and the Texas outline. Start and Results have no map; the map is the Live page's.

### Start: year and preset

The run settings card and "Change settings" have a Year setting, 2025 or 2026 (from `years` in `GET /settings`;
2024 is never offered). Picking a year offers that year's presets (each has "From January 1"); 2026 shows its
one-line description under it, since its prices are real up to a date and the AI's prediction after it. The
reading is in `src/api/settings.ts` (tested), which tolerates a backend without `years`.

### Results: "What the AI adds"

Right under the headline row, from `GET /results/ai-gains`: what the AI forecast earns over the simple rule
("+$67 per battery per year (+54%) vs the simple rule, $1.35 million more a year for the 20,000-battery fleet",
built from `uplift` and the fleet size), then three cards, each with a "What this means" line: earns more, gets
closer to perfect (`share_of_best` as bars) and soaks up surplus power (`surplus_mwh`). It shows the pitch year
only (the first entry of `years` with `is_pitch: true`, 2025) and ignores any other year: there is no year
switch and no spike card. A 404 `no_results` hides the section. The model and every sentence
are in `src/screens/results/aiGains.ts` (tested).

- **Charts**: [uPlot](https://github.com/leeoniya/uPlot) (about 50 KB): canvas, fast with 2,000-point series,
  live appends, bands for the forecast, draw hooks for the charge/sell shading and crash markers.
- **Performance**: a frame every second goes through a pure reducer into a tiny external store. Panels read
  single slices, so only panels whose data changed re-render; the charts subscribe outside React and call
  `setData`. History is capped at 2,000 ticks and events at 50.
- **Live connection**: reconnects with exponential backoff and jitter (Cloud Run closes WebSockets after 60
  minutes, so a long-lived connection that closes comes back within about half a second). A watchdog closes a
  socket that is quiet for 15 s (idle messages come every 5 s), and a handshake that has not finished after 15 s counts as a failed attempt.
  After three failed WebSocket attempts it polls `GET /state` every 2 s and keeps retrying the WebSocket every
  30 s.
- **Accessibility**: keyboard-only use works throughout (skip link, native dialogs, radio groups, a real
  switch, city buttons and Esc on the map); visible focus rings; AA contrast in the site's one dark theme
  (tested, including the primary button, the bars, the dot colours and the outline against the page); status never shown by color alone
  (icons and words next to every color); charts have text read-outs above them, and the map canvas has a text
  alternative with the current counts.
- **Language**: plain words by default; "Show technical details" (header, remembered per browser) reveals raw
  values and spec terms in brackets, e.g. "Controllers (worker processes)".
