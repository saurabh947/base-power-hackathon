# Notes: website (web/)

Owner: website builder. Everything lives in `web/`, plus `mk/web.mk` and this file. How to run and deploy it is
in `web/README.md`; the mock API is described in `web/mock/README.md`.

## What was built

- **One look for the whole site** (2026-09-26, the human: "change the results and the start page to match the theme
  of the live page"): every page, the header and the footer use the live map's theme, always dark whatever the
  browser's colour preference: the dark gray page (#25282c), compact translucent dark panels with light text and
  small spaced-capital titles like the map's corners, green for the primary action (Start, Watch live, Resume) and
  links, red only for failures and the map's Texas outline.
- **Start** (`/`): headline and subhead from docs/UX.md, with "20,000 home batteries" in a selling battery's green;
  the run-settings card above the big green "Start the simulation" button (Where: "Dallas–Fort Worth, Houston, San
  Antonio, and Austin", the Texas Triangle); a "Change settings" editor for exactly preset, speed, decision method
  and starting charge; "Waking up the simulator… (usually a few seconds)" while `GET /health` is slow or failing;
  "A simulation is running. Watch live" plus "Start over" when `GET /health` reports a run. One calm column,
  centred between the header and the footer; no map (the map is the Live page's).
- **Live** (`/live`) opens on the **live map** (docs/UX.md, "Live map"):
  - a full-window, always-dark gray page (#25282c) with Texas as a bright red outline and the 20,000 batteries as
    green dots from `GET /fleet/layout` and `battery_status`;
  - clickable Texas Triangle cities: label buttons beside each cluster, or click a cluster; the "All of Texas" chip,
    Esc or empty map go back; the selected city's dots stay bright and the others dim a little;
  - four compact translucent corners: top-left the replay clock and the price (all four cities, or the selected
    city's price with its ¢/kWh, a 6-hour mini line with charge/sell shading and the forecast band); top-right
    money earned (fleet or `totals.by_location`), the race, and power asked for vs delivered (numbers, sparkline,
    On target); bottom-left Pause/Resume, Speed, Reset, Take batteries offline, the presenter-only Crash a
    controller, "Show charts" and the Presenter link; bottom-right the 20 controllers (those serving the selected
    city highlighted, from `workers[].by_location`), batteries online and by state (doubling as the dot legend),
    and the last 3 events;
  - **the race** (2026-09-26, approved by the human): under the money, "RACE ↑ AI forecast ahead by $15,290"
    over two labelled bars, "AI forecast $46,310" in green and "Simple rule $31,020" in gray, from `race` in every
    frame (docs/API.md): a copy of the same batteries running the simple rule on the same prices. Selecting a city
    shows that city's race from `race.by_location`, with an arrow for what each side is doing there. The lead is
    green when the live strategy is ahead, plain text when the other one is, "Level so far" at a tie. Hidden while
    `race` is null (idle, or a backend that sends none). `race_update` events show in "What just happened" with a
    flag icon (and "Race update:" for screen readers), on the map and in "Show charts";
  - the caption "Battery locations are simulated inside real metro areas.";
  - "Show charts" switches to the detailed layout from before (top bar, price chart with a 4-city toggle that
    follows the map's selection, power chart, controllers, money, events, the availability drawer), in the same
    dark theme; "Show map" switches back. The choice is kept for the tab. Its money card has the race too: "Race
    against the simple rule", a one-line explainer, the two bars, the lead and the charge left on each side.
- **Results** (`/results`): the per-battery headline as a big green number, the fleet total, a row of three
  figures ($/kWh-year, $/kW-year, cycles a year); "By city" rows from `per_location_usd` (biggest first, a bar
  each); comparison bars (the headline method, the AI forecast, green; the other methods gray; "Best possible
  (perfect hindsight)" hatched); **What the AI adds** (below); top 10 days; "How we got this number"; "Re-run the
  full year" with progress.
- **What the AI adds** (Results, 2026-09-26, approved by the human): a full-width row right under the headline
  row, from `GET /results/ai-gains`:
  - the headline "+$67 per battery per year (+54%) vs the simple rule, $1.35 million more a year for the
    20,000-battery fleet", a big green "+$67" with a "+54%" chip, under the kicker "AI forecast (TimesFM-3),
    2025";
  - four cards, each with its figures as labelled bars (the AI's green) and a "What this means:" line:
    (a) **Earns more**: $191 vs $124 per battery a year, and the whole fleet "+$1.35 million a year ($3.82 million
    vs $2.47 million a year)"; (b) **Gets closer to perfect**: `share_of_best` for the simple rule, the evening
    peak and the AI (29% / 32% / 45%), "100% is $421 per battery per year", and that "perfect" means knowing every
    price in advance; (c) **Soaks up surplus power**: 7,011 vs 1,239 MWh bought at $0/MWh or less, "5.7× as much",
    "about what 190,000 homes use in a day", explained as power that was free or cheaper than free, often excess
    wind or solar; (d) **Ready for spikes**, always 2024 and tagged "2024": charge left when the spikes began as
    two battery gauges (52% vs 24%), "Delivered 611 MWh vs 76 MWh during the spikes (8×)" and "2024 had 53 price
    spikes of $1,000/MWh or more ($1.00 per kWh)";
  - a "Year" switch, "2025 (headline year)" first, then "2024 (check year)", with the line "The headline number
    above is for 2025. 2024 is a second full year, to check that the gain holds."; it drives the headline and
    cards a–c;
  - no "where it doesn't help" line, as the human asked; the fine print is unchanged;
  - a 404 `no_results` (or an answer with nothing usable) hides the section.
- **Mock** additions: the race (a shadow fleet on the comparison strategy) in snapshots and frames, `race_update`
  events at a spike's start and end, and `GET /results/ai-gains` with the measured numbers from RUNLOG.md; the
  pitch results' per-battery figures now come from the same measured run (the practice banner says so).
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
- **One theme, always dark**: `tokens.css` has a single `:root` set of tokens, the live map's, with `color-scheme:
  dark` (and `<meta name="color-scheme" content="dark">`, so form controls and the first paint are dark too); the
  former light, dark and `data-theme="map"` variants are gone, and the charts no longer rebuild on a
  prefers-color-scheme change. Panels everywhere use the map corners' `--panel` (translucent), `--panel-border`,
  12 px radius and shadow; their titles are small spaced capitals like the corners' labels. The header and the
  footer span the window on every page, as on the map, so nothing jumps when moving between pages. On the map the
  footer is still hidden (its Presenter link is in the controls corner) and toasts sit bottom-centre.
- **Colours with a job**: green (`--accent`, the map's battery green) is the primary action and links, with dark
  text on it; red stays for failures and the Texas outline (never the main action); neutral news (info notices and
  events) is blue, so it never reads as "good"; the keyboard focus ring is blue too, distinct from the green
  "selected" states. The big results number and "20,000 home batteries" on Start use a selling dot's bright green
  (`--money`, tested to equal `PALETTE.selling`). Every text pair, the primary button, the focus ring, the bars
  against their track and the map's dots and outline are AA-checked in `src/styles/contrast.test.ts`, which also
  checks that no colour-scheme or data-theme variant comes back and that every token the stylesheets use exists.
- **Start without a map**: a faint Texas outline with glowing cities was tried behind the Start content and removed
  at the human's request ("why is there a texas map on the start page? i think its repetitive"): the map belongs
  on the Live page. Start is one calm column instead, centred between the header and the footer, with the Start
  button above the fold at 1280 × 800 (even with the practice banner).
- **Results rows**: `src/screens/results/model.ts` (tested) orders the methods (simple rule, AI forecast, evening
  peak, then the hindsight ceiling), sizes the bars against the longest, and turns `per_location_usd` into "By city"
  rows, biggest first. Those are fleet totals per city (docs/API.md); the site does not divide them by a city's
  battery count, since the results do not say how the full-year run split the fleet. Close sums get two decimals
  ("$1.18 million" and "$1.24 million" rather than "$1.2 million" twice). When the comparison panel is taller than
  its bars (it matches the headline panel beside it), the bars spread out evenly.
- **Labels**: each city's label goes on the side that suits the geography (north of Dallas–Fort Worth, east of
  Houston, west of Austin, south-west of San Antonio) unless that would cover a corner panel, the chip, a notice,
  another label or another city's dots; placement re-runs when a panel changes size.
- **Narrow windows** (≤ 860 px): the map comes first and the corners stack below it.
- **Language details**: times say "Central time" (all four cities keep America/Chicago time), like the backend's
  event messages. City names come from `GET /fleet/layout` `cities[].name`, falling back to the documented four.
- **Re-renders**: the map stage does not re-render per frame; the canvas is fed outside React, and each corner
  reads its own slices.
- **What the AI adds, the numbers**: `src/screens/results/aiGains.ts` (tested) reads `GET /results/ai-gains`
  defensively (years without the AI or the baseline are skipped, the pitch year first) and writes every sentence.
  Nothing is hardcoded: the headline is `uplift` (per battery, `pct` as a fraction, `fleet_usd_year`), with any
  missing piece worked out from the two methods and the fleet size; the fleet size is `GET /results/pitch`'s
  `fleet_size`, else what the AI's own sums imply (fleet ÷ per battery). The spike card reads the 2024 entry of
  `years` (the count from the AI's `spike_events`, else the baseline's) and is hidden without it or when its
  numbers are missing. Money follows the site's style ("$1.35 million" rather than "$1.35M"); ratios drop a
  trailing ".0" ("8×", "5.7×"); a spike is "$1,000/MWh or more", since the API counts prices ≥ $1,000/MWh.
- **What the AI adds, the look**: one full-width panel (span 12) so the headline reads across the page; four cards
  of the same four parts (head, figure, details, meaning) aligned across the row with CSS subgrid, a step lighter
  than the panel (`--card`, AA-checked); bars for every comparison (the AI's green, like the comparison bars
  above), battery gauges for the charge levels; 4 cards a row from 1,101 px, 2 below, 1 on phones, where the
  year switch takes the full width and breaks after the year.
- **Year labels**: "2025 (headline year)" and "2024 (check year)" rather than the brief's example "pitch year",
  which is our word, not a visitor's; the note under the headline says what each year is. The switch starts on the
  pitch year.
- **No jump on Results**: the page waits for `GET /results/ai-gains` (at most 1.5 s) before showing the results
  grid, so the section does not push everything down just after the page appears. A 4xx hides the section; a
  network blip on a reload keeps what is on screen.
- **The race, the numbers**: `src/screens/live/race.ts` (tested). Labels are `race.label` and the live strategy's
  label (`settings.strategy`, or the other of bands/forecast when settings have not arrived), without their
  bracketed detail ("AI forecast"). For all of Texas the lead is `live_minus_race_usd`; for a city it is the
  difference of `totals.by_location` and `race.by_location` revenue. Cents while both sums are under $100 (the
  first minutes of a replay), whole dollars after; a lead under half a cent or dollar is "Level so far". The
  reducer keeps `race` from each snapshot and frame (the same object when unchanged) and drops it when idle, on a
  new run, or when a frame has none.
- **The race, the corner**: the 1280 × 800 map has little room on the right, so the corner race is three lines:
  "RACE" and the lead on one line, then the two bars; its one-line explainer and the charge levels are in its
  tooltip and read to screen readers. In windows up to 860 px tall the corners' gaps are 1 px tighter and the
  money corner's power line 34 px tall (was 56). As a guard, LiveMap caps the money corner at the room the fleet
  corner leaves (`--corner-tr-max`, after every render and on resize), so the two right-hand corners never overlap;
  the money corner scrolls only then (with "Show technical details" on at 1280 × 800).
- **Mock** (`web/mock`): real prices for all four load zones and all four presets (Houston/DFW values identical to
  the previous export); simulated metro positions (seeded Gaussian blobs around real suburbs, kept on land);
  contiguous controller blocks and round-robin hand-over like the backend; `battery_status` as of the latest tick,
  4 for the two missed ticks (so it misses code 4 above 2 ticks a second, like the backend); `totals.by_location`,
  `workers[].by_location`, `by_location` in worker events; `GET /fleet/layout` with an ETag. Never in `dist/`
  (`scripts/check-dist.mjs`).
- **Mock race and gains**: the race is a shadow fleet with its own charge levels, deciding with the same code on
  the same lagged prices and availability, without crashes or delivery noise. Its simulated "forecast" strategy now
  peeks at the current interval as well as the next 4 hours (a stand-in for a good forecast), so it sells into a
  spike as it happens; before, it sold 15 minutes late, after the summer spike had passed. On the default replay
  the simple rule leads until 9 pm and the AI overtakes it in the spike ($101,152 vs $16,449 at the end).
  `GET /results/ai-gains` serves the measured RUNLOG numbers as the backend would (`bought_mwh`, not in the
  table, is derived so that sold × average sell price − bought × average buy price = the fleet's earnings); the
  2025 spike count (21) is docs/API.md's example. The pitch results' per-battery figures, the ceiling and the AI's
  per-city figures now come from the same run, so the practice Results page is consistent; the banner says which
  figures are real.

## Measurements (mock, Chrome, 1280×800 and 1920×1080)

- **Frame**: 18.7 KB of JSON at 1 tick per frame, of which `battery_status` is 13,336 characters; about 1 KB
  deflated (a uniform `battery_status` deflates to 37 bytes). The snapshot on connect was 143 KB with 169 ticks of
  history. `GET /fleet/layout`: 106,668 characters of positions.
- **Per frame**: decode `battery_status` + re-count per cell + restyle 0.4–0.5 ms; paint 0.1–0.2 ms (slowest of the
  last 120 paints 0.4–0.6 ms, during transitions); building the cells after a resize or a new layout 2.5–4.7 ms.
  448 dots at both sizes.
- **Bundle**: 442.7 KB JS (146.6 KB gzip) and 45.5 KB CSS with "What the AI adds" and the race (425.7 KB, 141.3 KB
  and 39.6 KB before; 424.2 KB and 37.2 KB before the site-wide theme; 372.4 KB JS before the map).
- **Right-hand corners at 1280 × 800** (with the practice banner, a city selected): money corner with the race
  315 px, fleet corner 357 px, 21 px apart; all of Texas: 73 px apart. The race block is 59 px tall.
- **Results at 1280 × 800**: the "What the AI adds" panel is 642 px tall, so it fits one screen when scrolled to.

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
- **`race`** (snapshot and frames): read if it has a numeric `revenue_usd`; `null` or missing hides it. The
  comparison is named from `race.label` (fallback: the strategy's name); `live_minus_race_usd` is the fleet lead;
  `by_location[city]` gives a city's revenue, `charge_level` and `action`; the live side's charge and action come
  from the latest tick.
- **`race_update` events**: shown verbatim like every event, with a flag icon, coloured by `severity`.
- **`GET /results/ai-gains`**: `ai` and `baseline` pick the two strategies (default forecast and bands); the
  `is_pitch` year is "(headline year)", any other "(check year)"; `share_of_best`, `surplus_mwh`, `spike_*` as
  documented; 404 `no_results` hides the section.
- **Everything else** as before: settings item shapes, the idle message and `last_run`, `run_finished`, 404
  `no_results`, `skipped` reasons, amounts as fleet totals, `error_pct` as a percentage.

## Open issues

- The event-triggered flash relies on the client's replay of ownership. If a viewer joins after more than 50
  events (the kept history), earlier hand-overs are unknown and a later crash of an adopter flashes only its
  original block. It never flashes the wrong city for a first crash.
- The map was checked against the mock only; it was not run against the merged backend here.
- `prefers-reduced-motion` could not be emulated in the browser pane; that path is covered by unit tests (no
  glows) and by reading the renderer (no loop, no pulses).
- The race and "What the AI adds" were built against docs/API.md and the mock while the backend for them was being
  built; they have not been run against it. The mock's 2025 spike count (21) is docs/API.md's example value.
- With "Show technical details" on at 1280 × 800, the money corner is taller than the room the fleet corner
  leaves, so it scrolls (capped, never overlapping).
- The browser pane sometimes reports the page as hidden, and then no ResizeObserver fires; LiveMap therefore also
  re-measures the right-hand corners after every render.

## Requests for integrator

(The earlier requests about code 4, the merge's API additions, the four-city wording in docs/UX.md, WebSocket
compression and the Vercel set-up are done.)

1. **docs/UX.md** (2026-09-26): please add the two approved features and the rest of the one-theme update:
   - Results: after the comparison bars, **What the AI adds**: the headline "+$67 per battery per year (+54%) vs
     the simple rule, $1.35 million more a year for the 20,000-battery fleet" (from `uplift` and the fleet size);
     four cards with a "What this means" line each (earns more; gets closer to perfect, where "perfect" means
     knowing every price in advance; soaks up surplus power, bought when it was free or cheaper than free, often
     excess wind or solar; ready for spikes, always 2024 and labelled so); a "2025 (headline year) / 2024 (check
     year)" switch for the headline and the first three cards; no "where it doesn't help" line (the human's
     request). Also the "By city" rows from `per_location_usd`.
   - Live map, top-right corner: money earned, then **the race** ("RACE ↑ AI forecast ahead by $15,290" over two
     bars; per city from `race.by_location`; hidden while idle), then power. "What just happened": `race_update`
     events with a flag icon. "Show charts": the race in the money card.
   - Principle 6: green is also the primary action and links; blue is neutral information and the keyboard focus
     ring. Start: no map (the map belongs on Live).
2. **Backend, race**: please send `race` in `GET /state` too (it is the snapshot; polling clients read it), label
   the forecast strategy "AI forecast (TimesFM-3)" or similar (the map drops the bracket: "AI forecast"), and give
   the after-a-spike `race_update` severity `good` when the live strategy leads (the flag takes the severity's
   colour).
3. **Backend, ai-gains**: `spike_events` is the year's count, the same for every strategy (the site reads the AI's,
   else the baseline's); `uplift.pct` stays a fraction (0.544). If the gains may ever cover a fleet other than
   `GET /results/pitch`'s, add `fleet_size` to AiGains; today the site takes the pitch results' `fleet_size`,
   else fleet ÷ per battery.
4. **RUNLOG.md**: a short website entry for the two features (outside the website's files): what the Results
   section and the race show, and that the mock serves the measured numbers.
5. **After `POST /run/speed`**, `snapshot.speed_tps` and `settings.speed_tps` should both show the new speed (the
   site reads `speed_tps` first).
6. **Forecast label**: the site calls the band "AI forecast, likely range" and names TimesFM-3 only in the
   decision-method label from `GET /settings`/results; if the model switch moves to TimesFM 2.5, send the new label
   there and the site follows.
