# fleet-sim website

The public site for fleet-sim: replay real 2025 Texas (ERCOT) electricity prices and watch 20,000 simulated
home batteries buy cheap, sell high, and keep working when controllers crash. React + Vite + TypeScript,
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
replay preset (two days each); everything else it serves, including where the batteries sit, is **simulated**, and
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
| `npm test` | vitest: formatters, frame reducer, reconnect logic, settings, chart model, results rows, map (projection, outline, layout and status decoding, cells, looks, selection, labels), mock, AA contrast of the site's one theme |
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

Presenter mode needs no configuration here. The passcode lives in Secret Manager on the backend; a presenter
types it after clicking the small "Presenter" link in the footer. The site keeps it in `sessionStorage` only
(gone when the tab closes) and sends it as the `X-Presenter-Passcode` header.

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
6. Check: open the site, turn on "Show technical details" and look at the footer: it shows the API URL and the
   backend version. The Live screen's top bar should say "Live updates".

`vercel.json` also rewrites every path to `index.html` (the site uses `/`, `/live` and `/results`), caches the
hashed files in `/assets` for a year, and sets a few security headers. Vercel runs Node 22 per `engines`.

## How the site is built

```
src/
  api/          types.ts (the docs/API.md contract), client.ts (fetch + ApiError), errors.ts (friendly
                messages for 429/501/503/…), settings.ts (tolerant GET /settings reader), config.ts (base URL)
  live/         connection.ts (WebSocket + backoff + watchdog + GET /state polling fallback),
                reducer.ts (snapshot/frame/idle → view, capped, structurally shared), liveStore.ts
  map/          the live map's engine: albers.ts (EPSG:3083), texasOutline.ts (generated), geometry.ts,
                layout.ts (GET /fleet/layout), status.ts (battery_status nibbles), cells.ts (screen-space
                aggregation), style.ts (dot looks), renderer.ts (canvas), selection.ts, owners.ts and flash.ts
                (the crash flash from events), feed.ts
  lib/          format.ts (units + plain-language translations), cities.ts (names from the layout), fleet.ts,
                store.ts, router.ts, contrast.ts
  state/        backend health / wake-up, technical-details switch, presenter passcode, toasts
  components/   Panel ("What is this?" + "How it works"), Tech/Term, Dialog, charts/ (uPlot)
  screens/      start/, live/ (map/ is the default view, the charts layout is behind "Show charts"), results/
  styles/       tokens.css (the one always-dark theme, AA-checked), app.css, map.css
scripts/        check-dist.mjs (no mock in dist/), texas-outline.ts (builds src/map/texasOutline.ts)
mock/           simulated API for dev and tests only (never in dist/)
```

### The live map

The Live screen opens on a full-window map (docs/UX.md, "Live map"): a dark gray page, Texas as a bright red
outline, and the 20,000 batteries as green dots, with the four corners holding the clock and price, money and
power, the controls, and the controllers, batteries and latest events. "Show charts" switches to the detailed
layout (remembered for the tab); "Show map" switches back.

- **Projection.** `src/map/albers.ts` implements the ellipsoidal Albers equal-area conic (Snyder 1987) with the
  EPSG:3083 parameters; it matches PROJ's reference values. The outline and the dots use the same function.
- **Outline.** `src/map/texasOutline.ts` is generated by `scripts/texas-outline.ts` from the U.S. Census Bureau
  2023 cartographic boundary file (states, 1:5,000,000, public domain): the Texas ring, projected, simplified with
  Douglas–Peucker at 2 km to 351 points, and checked to be a simple polygon. `make web-texas` downloads the file
  to a temp dir and rebuilds it.
- **Dots.** Batteries are binned into a hexagonal grid in screen pixels (about 140 cells top to bottom, so a few
  hundred dots at any size); each dot's area is proportional to its battery count and it sits between its
  batteries' centroid and its hex centre. Each dot mixes its batteries' looks: green (medium when waiting,
  bright with a soft glow when selling, a light ring when charging), dim gray when offline.
- **Controller down (code 4).** Each dot gets a dark core whose area is the share of its batteries that lost
  their controller, pulsing; when the other controllers take them over, the cores close and those dots briefly
  light up green. A controller runs 1,000 batteries spread over its city, so the flash shows in that city without
  blacking it out for a 12% event. Code 4 lasts about two ticks, so at 5-10 ticks a second a frame can miss it:
  then the `worker_suspect`/`worker_dead`/`worker_adopted` events trigger the same flash on that controller's
  batteries, which `src/map/owners.ts` replays from the default blocks and each hand-over's `adopted_by`.
- **Drawing.** Two canvases: the outline (redrawn on resize) and the dots (redrawn on each 1 Hz frame).
  `requestAnimationFrame` runs only while a transition, a flash or a re-light plays. `prefers-reduced-motion`
  turns off glows, pulses and transitions.
- **Cities.** Labels are real buttons placed beside each cluster (avoiding the corner panels); clicking a city's
  dots also selects it; the "All of Texas" chip, Esc or clicking empty map go back. The corners then show that
  city: per-location tick values, `totals.by_location`, `availability.by_location`, and the controllers with
  batteries there (`workers[].by_location`) highlighted.
- **Always dark, everywhere.** The map's look is the whole site's: one set of tokens (`src/styles/tokens.css`),
  always dark whatever the browser's colour preference, with the same translucent panels on every page, green for
  the primary action and links, and red only for failures and the Texas outline. Start and Results have no map;
  the map is the Live page's.

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
