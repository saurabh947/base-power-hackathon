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
whole site works offline. The mock replays **real** ERCOT prices for May 16–17, 2025; everything else it serves
is **simulated**, and the site shows a "Practice data" banner while connected to it. See
[`mock/README.md`](mock/README.md).

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
and `make web-check` (see `mk/web.mk`).

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server; mock API at `/__mock` unless `VITE_API_BASE_URL` is set |
| `npm run dev:local` | Dev server against `http://localhost:8000` |
| `npm run mock` | Standalone mock API on port 8787 (`MOCK_PORT` to change) |
| `npm run typecheck` | `tsc -b` over the app, the mock and the configs |
| `npm run lint` | oxlint (correctness, suspicious, React hooks, jsx-a11y), warnings fail |
| `npm test` | vitest: formatters, frame reducer, reconnect logic, settings, chart model, mock, AA contrast |
| `npm run build` | typecheck, `vite build` to `dist/`, then `scripts/check-dist.mjs` fails if any mock code shipped |
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
  lib/          format.ts (units + plain-language translations), store.ts, router.ts, contrast.ts
  state/        backend health / wake-up, technical-details switch, presenter passcode, toasts
  components/   Panel ("What is this?" + "How it works"), Tech/Term, Dialog, charts/ (uPlot)
  screens/      start/, live/, results/
  styles/       tokens.css (light + dark colors, AA-checked), app.css
mock/           simulated API for dev and tests only (never in dist/)
```

- **Charts**: [uPlot](https://github.com/leeoniya/uPlot) (about 50 KB): canvas, fast with 2,000-point series,
  live appends, bands for the forecast, draw hooks for the charge/sell shading and crash markers.
- **Performance**: a frame every second goes through a pure reducer into a tiny external store. Panels read
  single slices, so only panels whose data changed re-render; the charts subscribe outside React and call
  `setData`. History is capped at 2,000 ticks and events at 50.
- **Live connection**: reconnects with exponential backoff and jitter (Cloud Run closes WebSockets after 60
  minutes, so a long-lived connection that closes comes back within about half a second). A watchdog closes a
  socket that is quiet for 10 s. After three failed WebSocket attempts it polls `GET /state` every 2 s and keeps
  retrying the WebSocket every 30 s.
- **Accessibility**: keyboard-only use works throughout (skip link, native dialogs, radio groups, a real
  switch); visible focus rings; AA contrast in light and dark (tested); status never shown by color alone
  (icons and words next to every color); charts have text read-outs above them.
- **Language**: plain words by default; "Show technical details" (header, remembered per browser) reveals raw
  values and spec terms in brackets, e.g. "Controllers (worker processes)".
