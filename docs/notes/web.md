# Notes: website (web/)

Owner: website builder. Everything lives in `web/`, plus `mk/web.mk` and this file. How to run and deploy it is
in `web/README.md`; the mock API is described in `web/mock/README.md`.

## What was built

- **Live map v2 and the human's other requests** (2026-09-26; docs/UX.md "Live map v2", contract docs/API.md at
  4879fde). Each piece, in the order the human asked:
  1. **No presenter mode.** The header link, the passcode dialog, the sessionStorage passcode,
     `X-Presenter-Passcode`, `POST /presenter/check`, `GET /batteries` ("Find a battery"), their tests and docs
     are gone. "Crash a controller" is a normal button in the Live controls (the map's bottom-left corner and the
     charts layout's top bar) that crashes a random working controller. A visitor can also click a controller
     tile (map corner or "Show charts"): a small popover above it asks "Crash controller 7?", says "Its 1,000
     batteries in Houston pulse red on the map until the other controllers take them over.", and offers "Crash
     it" / "Cancel" (Esc, a click elsewhere or tabbing away closes it; focus returns to the tile). One crash every
     2 s for everyone: after a crash, or a 429 with `retry_after_s`, the buttons read "Next crash in 2 s" / "Wait
     2 s" and are disabled until then. Errors in plain words: 429 "One crash every 2 seconds, please."; 409
     `already_dead` "That controller has already crashed."; 409 `too_few_healthy` "At least 2 controllers must
     keep working." (with "Press Reset to bring the crashed controllers back"); 404 `unknown_worker` "There is no
     such controller." (`src/screens/live/crash.ts`, `src/api/errors.ts`, tested).
  2. **Year on Start**: the run-settings card has a Year row and "Change settings" a Year switch, **2025 / 2026**
     only (from `years` in `GET /settings`; earlier years and years without presets are never offered). Picking a
     year offers that year's presets, each year with "From January 1"; picking a preset picks its year. For 2026
     the year's `description` shows as one line under it ("Real prices through Sep 20, then the AI's
     prediction."). A preset label that already names its date is not repeated ("From January 1, 2026", not
     "From January 1, Jan 1, 2026").
  3. **2026 prediction on Live**: on ticks with `predicted: true` the clock gets a violet "Predicted" badge (AI
     sparkle; its tooltip says the prices are the AI's prediction, not real ERCOT data); the timeline and the
     "Show charts" price chart shade the predicted part violet ("AI prediction →"); money earned on predicted
     ticks is labelled "projected" ("$6,790 of it is projected, on the AI's predicted prices", in the map's money
     corner, the money card and the finished notice; in a short window the corner's line ends at "projected" and
     the reason is in its tooltip); `prediction_started` shows in both event feeds with the AI
     icon ("Prediction:" for screen readers). The replay label says whose prices are playing.
  4. **Dot colours** (docs/UX.md "Live map v2"): one meaning per look, shape as well as colour: green solid with a
     soft halo = selling; blue solid = charging; gray and small = waiting; red hollow ring = offline; red solid and
     pulsing = its controller crashed, back to its normal look (with a short ring) after the hand-over. Offline and
     crashed batteries are drawn red where they are (the slider's random subset, the controller's block). A dot
     takes its batteries' majority look, and any offline or crashed battery in it adds a red edge. The
     bottom-right counts are the legend, each next to the same dot, plus "red edge: some offline or crashed". The
     human's bug (offline or crashed batteries did not turn red) is fixed, while paused too; root cause below.
  5. **Availability drawer**: "Find a battery" is gone (code and tests); "Bring every battery back online" is at
     the top, next to the count offline. The drawer opens on the LEFT with a light backdrop, so the four cities
     stay visible while their dots turn red; its explainer says offline batteries show as red rings.
  6. **Race, third bar**: when `race.shows_potential` is true, "All batteries online (projected)" from
     `race.potential`, with the shortfall "Outages cost $790 so far" ("Outages have cost nothing so far" at $0),
     for all of Texas or the selected city (`potential.by_location`). In the map corner it is compact: a third
     bar labelled "All online" and one line "projected · Outages cost $790 so far"; the money card has the full
     label and a sentence.
  7. **Timeline** (top-left corner, 30 px track): a mini price line for the run so far (all four cities weighted
     by batteries, or the selected city), green bands where the fleet sold, blue where it bought, ▲/▼ at each
     day's high and low, the prediction shaded, and an honest caption ("Real prices from 6 pm, May 30", "Real
     prices, then the AI's (shaded)", "The AI's predicted prices (shaded)"). Dragging, ←/→ (Shift or Page Up/Down
     for an hour; Home, End) show the map and every map corner as they were, with a "Viewing 7:45 pm · Back to
     live" chip; the simulation keeps running, and "Show charts" and the controls stay live. Past dots come from
     the frames this page recorded (`battery_status`, 8 MB cap); before the page was opened the corners work
     from the tick history, the map shows each city's action only, and the chip says so in one line ("Before you
     opened this page: the map shows each city as a whole, not each battery.").
  8. **Zoom, exactly one level**: the map loads on all of Texas; "+" in the scope pill (next to "All of Texas"),
     a double-click or the + key zooms smoothly (650 ms) to the Texas Triangle, the box around the four metros'
     batteries with a margin, centred between the corner panels; the dots re-aggregate at the new scale (more,
     smaller cells), labels move with it; "−", Esc, the − key or another double-click zooms out.
  9. **Mock**: years 2025/2026 with presets per year (each with "From January 1") and a 2026 that is 2025's real
     prices a year on, then a simulated prediction after Sep 20, labelled practice data; `predicted` ticks and
     `prediction_started`; `race.potential` / `shows_potential`; the public, rate-limited `POST /chaos/kill` with
     optional `worker_id`; `battery_status` that shows availability changes and crashes within one frame while
     paused; ai-gains for 2025 only; no presenter passcode.
  10. **Results: no 2024** (the human: "remove the 2024 switch too"): "What the AI adds" shows the pitch year only
     (the first `years` entry with `is_pitch: true`, 2025) and ignores any other year; the year switch, the
     2024 note under the headline and the "Ready for spikes" card are gone. Three cards: Earns more, Gets closer
     to perfect, Soaks up surplus power, three across from 861 px (one per row below), their parts aligned.
- **One look for the whole site** (2026-09-26, the human: "change the results and the start page to match the theme
  of the live page"): every page and the header use the live map's theme, always dark whatever the browser's
  colour preference: the dark gray page (#25282c), compact translucent dark panels with light text and small
  spaced-capital titles like the map's corners; green for selling and the primary action (Start, Watch live,
  Resume), blue for charging, violet for the AI's forecast and prediction, red for outages, failures and the
  Texas outline.
- **Header, no footer** (2026-09-26, the human): the brand, Start / Live / Results, and on the right the "Show
  technical details" switch (with technical details on, also the API URL and backend version, shortened with "…"
  when the header is full). No footer on any page; no presenter link. The mock-only "Practice data" banner and the
  map caption stay.
- **Start** (`/`): headline and subhead from docs/UX.md, with "20,000 home batteries" in a selling battery's green;
  the run-settings card above the big green "Start the simulation" button (Where: "Dallas–Fort Worth, Houston, San
  Antonio, and Austin", the Texas Triangle); a "Change settings" editor for exactly year, preset, speed, decision
  method and starting charge; "Waking up the simulator… (usually a few seconds)" while `GET /health` is slow or
  failing; "A simulation is running. Watch live" plus "Start over" when `GET /health` reports a run. One calm
  column, centred under the header; no map (the map is the Live page's).
- **Live** (`/live`) opens on the **live map** (docs/UX.md, "Live map" and "Live map v2"):
  - a full-window, always-dark gray page (#25282c) with Texas as a bright red outline and the 20,000 batteries as
    dots from `GET /fleet/layout` and `battery_status`, in the v2 looks above;
  - clickable Texas Triangle cities: label buttons beside each cluster, or click a cluster; the "All of Texas" chip,
    Esc or empty map go back; the selected city's dots stay bright and the others dim a little;
  - four compact translucent corners: top-left the run status, the replay clock (with "Predicted"), the timeline
    and the price (all four cities, or the selected city's price with its ¢/kWh, a 6-hour mini line with
    charge/sell shading and the forecast band); top-right money earned (fleet or `totals.by_location`, with the
    projected part), the race, and power asked for vs delivered (numbers, sparkline, On target); bottom-left
    Pause/Resume, Speed, Reset, Take batteries offline, Crash a controller ("or click a controller") and "Show
    charts"; bottom-right the 20 controllers (those serving the selected city highlighted, from
    `workers[].by_location`; click one to crash it), batteries online and by state (the dot legend), and the last
    3 events;
  - **the race** (2026-09-26, approved by the human): under the money, "RACE ↑ AI forecast ahead by $15,290"
    over two labelled bars, "AI forecast $46,310" in green and "Simple rule $31,020" in gray, from `race` in every
    frame (docs/API.md), plus the all-online third bar after an outage. Selecting a city shows that city's race
    from `race.by_location`, with an arrow for what each side is doing there. The lead is green when the live
    strategy is ahead, plain text when the other one is, "Level so far" at a tie. Hidden while `race` is null.
    `race_update` events show in "What just happened" with a flag icon, on the map and in "Show charts";
  - the caption "Battery locations are simulated inside real metro areas.";
  - "Show charts" switches to the detailed layout (top bar, price chart with a 4-city toggle that follows the
    map's selection, power chart, controllers, money, events, the availability drawer), in the same dark theme;
    "Show map" switches back. The choice is kept for the tab. Its money card has the race too.
- **Results** (`/results`): the per-battery headline as a big green number, the fleet total, a row of three
  figures ($/kWh-year, $/kW-year, cycles a year); "By city" rows from `per_location_usd` (biggest first, a bar
  each); comparison bars (the headline method, the AI forecast, green; the other methods gray; "Best possible
  (perfect hindsight)" hatched); **What the AI adds** (below); top 10 days; "How we got this number"; "Re-run the
  full year" with progress and Stop.
- **What the AI adds** (Results, 2026-09-26, approved by the human; 2024 removed the same day): a full-width row
  right under the headline row, from `GET /results/ai-gains`, for the pitch year (2025) only:
  - the headline "+$67 per battery per year (+54%) vs the simple rule, $1.35 million more a year for the
    20,000-battery fleet", a big green "+$67" with a "+54%" chip, under the kicker "AI forecast (TimesFM-3),
    2025";
  - three cards, each with its figures as labelled bars (the AI's green) and a "What this means:" line:
    (a) **Earns more**: $191 vs $124 per battery a year, and the whole fleet "+$1.35 million a year ($3.82 million
    vs $2.47 million a year)"; (b) **Gets closer to perfect**: `share_of_best` for the simple rule, the evening
    peak and the AI (29% / 32% / 45%), "100% is $421 per battery per year", and that "perfect" means knowing every
    price in advance; (c) **Soaks up surplus power**: 7,011 vs 1,239 MWh bought at $0/MWh or less, "5.7× as much",
    "about what 190,000 homes use in a day", explained as power that was free or cheaper than free, often excess
    wind or solar;
  - no year switch, no 2024 note, no "Ready for spikes" card, no "where it doesn't help" line; the fine print is
    unchanged;
  - a 404 `no_results` (or an answer with nothing usable) hides the section.
- **Re-run the full year, Stop** (2026-09-26, the human): a "Stop" button next to "Re-run the full year", enabled
  only while a re-run runs. It sends `POST /backtest/{job_id}/cancel`: on 200 it says "Stopped. The previous
  results are still shown." (or "Stopped. No full-year results were saved." when there were none) and the page
  keeps its results; on 409 `not_running` the next status poll says how the run ended; on 404 the job is
  forgotten; if the simulator cannot be reached the re-run goes on and a plain error shows. A polled status of
  `cancelled` (someone else's Stop: the re-run is shared) shows the same message. `src/screens/results/rerun.ts`
  (tested).
- **Language**: every panel has a one-line "What is this?" and an optional "How it works"; "Show technical details"
  reveals raw values and spec terms in brackets (on the map too: location ids, raw kW, error_pct, status codes,
  and the map's own sizes and timings); numbers carry units and a human translation.

## The red-dots bug: root cause

The human took batteries offline (and crashed a controller) and the dots did not turn red. Four causes, three on
the website:

1. **Blending hid minorities.** Each dot mixed its batteries' colours by count, so 30% offline moved a dot 30%
   of the way towards the offline colour, and a crashed controller's 1,000 batteries, spread over its city, were
   about 12–14% of each dot there: a barely visible tint.
2. **Offline was not red.** The old look drew offline as dim gray, which reads as "waiting", not as an outage; a
   crash was a small proportional dark core, and the event-triggered flash lasted one frame.
3. **The drawer covered the dots.** The availability drawer opened on the right with a dimming backdrop, over
   the eastern half of Texas where the batteries are, so the change happened out of sight.
4. **Backend, while paused**: `battery_status` changed only at ticks, so a crash (and, on the real backend, an
   availability change) while paused never reached the map. docs/API.md now asks for it within one frame; the
   backend is fixing it, and the mock does it.

The site's data flow was fine (it decodes every frame's `battery_status`, paused or not). The fix is the v2 look:
majority looks, hollow red rings for offline, solid pulsing red for crashed, and a red edge on any dot with an
outage whose width (1–2.2 px) and strength (45–100%) grow with the share, all drawn where the batteries are; plus
the drawer on the left with a light backdrop. Checked in the browser, paused, 40% of Houston offline: red dot
pixels went from 0 to 5,177 within 1.4 s and the legend read "2,800 offline"; a crash while paused showed "1,000
controller crashed" pulsing red, then 0 after the hand-over.

## Decisions

- **Stack**: React 19, Vite 8, TypeScript 6.0, uPlot for the detailed charts, plain canvas and SVG for the map,
  oxlint, vitest. Runtime dependencies: react, react-dom, uplot. No map library: the projection, the outline, the
  aggregation and the zoom are a few hundred lines of tested TypeScript.
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
  left-hand ones sit over New Mexico and Mexico. The scope pill ("All of Texas", the hint, + / −) sits over
  Oklahoma, right of the panhandle.
- **Dots**: batteries are binned into a hexagonal grid in screen pixels, about 95 cells from top to bottom (6–13
  px), so the count is the same at any size. A dot's area is proportional to its batteries (below 0.9 px a dot
  keeps the radius and fades, so its ink still matches its count). Each dot sits halfway between its batteries'
  centroid and its hex centre.
- **Dot looks (v2)**, `src/map/looks.ts` (tested): the majority code decides the look, ties going to the outage
  (controller down, then offline), then selling, charging, waiting, so a half-offline dot is a ring. Waiting dots
  are drawn at 60% of the radius ("gray, small"); a ring is at least 2.4 px so it never reads as a dot. The red
  edge only goes on dots whose majority is not an outage, 1–2.2 px wide and 45–100% strong with the share, so
  even one offline battery in a cell shows. Colours: selling `--money` green (#80ffa6), charging blue (#54a0ff),
  waiting gray (#848c96), outages red (#ff5a5a); all ≥ 3:1 against the page and the legend uses the same values
  (tested). Other cities' dots dim to 38% while one is selected.
- **Controller down (v2)**: the batteries of a crashed controller are drawn solid red and pulse (a 900 ms beat
  between 45% and 100%, never gone) until the hand-over; then they return to their look with a 900 ms ring.
  Code 4 lasts about two ticks, so above 2 ticks a second a 1 s frame can miss it. The worker events cannot be
  missed: on `worker_suspect`, `worker_dead` or `worker_adopted` with no code 4 in the frame (and none shown in
  the last 2.5 s), that controller's batteries are shown as down for that frame, and the next frame brings them
  back with the ring. Which batteries a controller runs is replayed on the client (`src/map/owners.ts`): the
  default blocks (numpy `array_split`) and each hand-over's `adopted_by`, round-robin in id order as
  `coordinator._reassign` does. Events older than 60 s (a reconnect replaying history) don't trigger it.
- **Zoom** (`src/map/viewports.ts`, tested): the Triangle is the box around the batteries, ignoring the farthest
  0.2% on each side (one stray position cannot stretch it), plus 8% of its larger side (at least 20 km), fitted
  between the corner panels. Between the two views the map scales about the one screen point both share, so it
  zooms straight in without sliding sideways; the outline is redrawn per frame of the zoom, and the dots crossfade
  from the old cells to the new ones (built once at the start, for the target scale, at 1.3× the home spacing so
  the metros get more, clearly separate dots). Labels are hidden during the 650 ms and placed again at the end.
  The zoom buttons sit in the scope pill because the top edge between the corners is the only free space at
  1280 × 800. Zoomed in, a notice moves to a compact bar at the bottom centre. `prefers-reduced-motion`: instant.
- **Timeline, the model** (`src/live/recorder.ts`, `src/live/past.ts`, `timeline.ts`, all tested): the recorder
  keeps, per frame, the packed `battery_status` bytes (10 KB for 20,000 batteries, shared while unchanged, e.g.
  paused), the controllers, availability and race, keyed by the sim time of the frame's latest tick. Past 8 MB
  (about 800 changed frames, 13 minutes at a frame a second) every other record in the older half is dropped, so
  the whole visit stays scrubbable, coarser further back; leaving the Live page marks a gap. The past view is
  rebuilt from the tick history: clock, prices, power, and money as today's totals minus what the later ticks
  earned; controllers, availability and race from the nearest recorded frame, or before the page was opened, the
  controllers replayed from the worker events and the rest unknown. The track's high/low markers are per day.
- **Timeline, the UI**: the map corners read the viewed moment through `LiveDataContext` (`useLiveData`), so the
  map, the clock, prices, money, the race, controllers and the legend all time-travel together while the
  controls, the toasts and "Show charts" stay live. The simulation is never paused by scrubbing. The chip sits
  at the top centre; its "Back to live" keeps keyboard focus on the timeline. ← / → work from anywhere on the map
  page (not in inputs), as on the track.
- **Crashes** (`src/screens/live/crash.ts`): one shared store (busy, wait-until) drives the corner button, the
  top-bar button and every tile's popover, so all of them count down together; the count-down starts from the
  server's `retry_after_s` when there is one. The popover is anchored above the clicked tile inside the tiles'
  own box (not a modal), because the map must stay visible to watch the dots turn red.
- **Years** (`src/api/settings.ts`, tested): a preset's year is its `year`, else its Central-time date (an older
  backend); only years from 2025 that have presets are offered, and a year a preset names but `years` omits still
  appears, so no preset is lost. The Year control uses the same segmented control as the other settings.
- **2026 on Live**: "projected" is worked out from the ticks marked `predicted` (their revenue summed), per city
  too; when the kept history itself starts inside the prediction (older ticks dropped), the sum would be short, so
  it says "Part of it is projected" instead. The price chart's violet wash starts at the first predicted tick. The timeline's caption says whose prices it shows, since the track looks the same
  either way.
- **Drawing**: two canvases, the outline (glow, redrawn on resize and during the zoom) and the dots (redrawn on
  every frame). Between frames nothing is drawn unless a transition (160 ms), a pulse, a zoom or a re-light
  (900 ms) plays; then a `requestAnimationFrame` loop runs until everything settles, and stops (a pulse keeps it
  going while any battery is down). `prefers-reduced-motion` turns off halos, pulses, transitions and the zoom
  animation.
- **Data flow**: `battery_status` is kept as the raw string in the live state; `src/map/feed.ts` decodes it once
  per frame (with the layout's city blocks) into codes and per-city counts shared by the canvas and the corners,
  or, while viewing the past, decodes the recorded bytes instead. The layout is fetched once while a map is on
  screen and revalidated with its ETag; a fleet-size change refetches it. With no `battery_status` at all (an older
  backend, or before the page was opened) each city's dots show its current action.
- **One theme, always dark**: `tokens.css` has a single `:root` set of tokens, the live map's, with `color-scheme:
  dark` (and `<meta name="color-scheme" content="dark">`). Panels everywhere use the map corners' `--panel`
  (translucent), `--panel-border`, 12 px radius and shadow; their titles are small spaced capitals like the
  corners' labels. The header spans the window on every page. On the map toasts sit bottom-centre.
- **Colours with a job**: green is selling, money and the primary action and links, with dark text on it; blue
  (#54a0ff) is charging everywhere (map, charts, action chips, timeline), a lighter blue (#8ab4ff) neutral news and
  the keyboard focus ring; violet is the AI's forecast band and its prediction; red is outages (#ff5a5a), failures
  and the Texas outline (never the main action). The big results number and "20,000 home batteries" on Start use
  a selling dot's bright green (`--money`, tested to equal `PALETTE.selling`). Every text pair, the primary
  button, the focus ring, the bars against their track and the map's dots and outline are AA-checked in
  `src/styles/contrast.test.ts`, which also checks that no colour-scheme or data-theme variant comes back and that
  every token the stylesheets use exists.
- **Start without a map**: a faint Texas outline was tried behind the Start content and removed at the human's
  request: the map belongs on the Live page. Start is one calm column, with the Start button above the fold at
  1280 × 800 (with the practice banner and the 2026 note too).
- **Results rows**: `src/screens/results/model.ts` (tested) orders the methods (simple rule, AI forecast, evening
  peak, then the hindsight ceiling), sizes the bars against the longest, and turns `per_location_usd` into "By city"
  rows, biggest first. Those are fleet totals per city (docs/API.md); the site does not divide them by a city's
  battery count. Close sums get two decimals ("$1.18 million" and "$1.24 million" rather than "$1.2 million"
  twice). When the comparison panel is taller than its bars, the bars spread out evenly.
- **Labels**: each city's label goes on the side that suits the geography (north of Dallas–Fort Worth, east of
  Houston, west of Austin, south-west of San Antonio) unless that would cover a corner panel, the scope pill, the
  viewing chip, a notice, another label or another city's dots; placement re-runs when a panel changes size and
  after a zoom.
- **Narrow windows** (≤ 860 px): the map comes first and the corners stack below it; the phone hint is hidden and
  notices are static.
- **Language details**: times say "Central time" (all four cities keep America/Chicago time), like the backend's
  event messages. City names come from `GET /fleet/layout` `cities[].name`, falling back to the documented four.
- **Re-renders**: the map stage does not re-render per frame; the canvas is fed outside React, and each corner
  reads its own slices.
- **What the AI adds, the numbers**: `src/screens/results/aiGains.ts` (tested) reads `GET /results/ai-gains`
  defensively: the pitch year is the first entry with `is_pitch: true` (with none flagged, older data, the first
  usable entry), every other year is ignored, and the section is hidden when the pitch year lacks the AI or the
  baseline. Nothing is hardcoded: the headline is `uplift` (per battery, `pct` as a fraction, `fleet_usd_year`),
  with any missing piece worked out from the two methods and the fleet size; the fleet size is
  `GET /results/pitch`'s `fleet_size`, else what the AI's own sums imply. Money follows the site's style
  ("$1.35 million"); ratios drop a trailing ".0" ("5.7×").
- **What the AI adds, the look**: one full-width panel (span 12) so the headline reads across the page; three
  cards of the same four parts (head, figure, details, meaning) aligned across the row with CSS subgrid, a step
  lighter than the panel (`--card`, AA-checked), three across from 861 px, one per row below, with the same 14 px
  gaps as the rest of the page.
- **No jump on Results**: the page waits for `GET /results/ai-gains` (at most 1.5 s) before showing the results
  grid. A 4xx hides the section; a network blip on a reload keeps what is on screen.
- **The race, the numbers**: `src/screens/live/race.ts` (tested). Labels are `race.label` and the live strategy's
  label, without their bracketed detail ("AI forecast"). For all of Texas the lead is `live_minus_race_usd`; for a
  city it is the difference of `totals.by_location` and `race.by_location` revenue. Cents while both sums are
  under $100, whole dollars after; a lead under half a cent or dollar is "Level so far". The third bar uses the
  site's own label, "All batteries online (projected)" (the map's corner: "All online"), not `potential.label`,
  and `shortfall_usd` for the loss (a city's: its potential minus its live revenue). The reducer keeps `race` from
  each snapshot and frame (the same object when unchanged) and drops it when idle, on a new run, or when a frame
  has none.
- **The race, the corner**: the 1280 × 800 map has little room on the right, so the corner race is compact: "RACE"
  and the lead on one line, then the bars, with the potential's note on one line; the explainer and charge levels
  are in the tooltip and read to screen readers. In windows up to 860 px tall the money's definition ("Gross
  wholesale value.") and the projection's reason move to tooltips, so each is one line. As a guard, LiveMap caps
  the money corner at the room the fleet corner leaves (`--corner-tr-max`), so the two right-hand corners never
  overlap; the money corner scrolls only if that is not enough.
- **Mock** (`web/mock`): real prices for all four load zones and every 2025 preset, plus Sep 14–20, 2025 as
  2026's stand-in; simulated metro positions; contiguous controller blocks and round-robin hand-over like the
  backend; `battery_status` as of the latest tick, with availability and (while paused) crashes applied at once;
  a race shadow fleet and a second, all-online one for `potential`; the 2026 prediction for two days after the
  real prices end, from the median of the week before, smoothed (plausible, labelled practice data); `GET
  /results/ai-gains` with the measured 2025 numbers from RUNLOG.md. Never in `dist/` (`scripts/check-dist.mjs`).
  Tested in `mock/sim.test.ts` and `mock/v2.test.ts` (which also calls the public crash over HTTP).

## Measurements (mock, Chrome)

- **Bundle**: 468.5 KB JS (155.2 KB gzip) and 50.9 KB CSS (11.0 KB gzip) with v2 (442.3 KB, 146.3 KB and 45.2 KB
  before; 372.4 KB JS before the map). v2 adds about 26 KB of JS net (the timeline, the recorder, the zoom, the
  crash popover and the years, minus presenter mode, "Find a battery" and the spike card).
- **Frame**: 18.7 KB of JSON at 1 tick per frame, of which `battery_status` is 13,336 characters; about 1 KB
  deflated. `GET /fleet/layout`: 106,668 characters of positions. The recorder stores 10 KB per changed frame.
- **The red-dots check** (paused, 40% of Houston offline): 0 → 5,177 red dot pixels within 1.4 s, the rest of the
  dots unchanged (5,184 → 5,096); legend "17,200 waiting · 2,800 offline".
- **Right-hand corners at 1280 × 800**, the worst case (practice banner, a 2026 run in its prediction with the
  projected line, Houston selected, the third race bar showing): the money corner's content is 323 px and fits
  above the 354 px fleet corner with no scrolling (before the one-line notes it needed 358 px and scrolled by
  31 px). At 1920 × 1080 the full wording shows with room to spare.
- **Results at 1280 × 800**: the three-card "What the AI adds" panel is 559 px tall (the four-card one with the
  year switch was 642 px), the cards 389 × 308 px, so it fits one screen when scrolled to.

## How the site follows the contract (docs/API.md, 4879fde)

- **Controllers**: `worker_id` 0–19 is shown as "Controller N" with N = worker_id + 1 everywhere. Event messages
  are shown verbatim. Crash markers and the "Latest handover" line read `detail.controller`, `detail.adopted_by`
  and `detail.ms`; the map reads `detail.worker_id`.
- **`POST /chaos/kill`**: public, no passcode, no body; `?worker_id=N` when a tile was clicked. 200 → the button
  waits 2 s; 429 → "One crash every 2 seconds, please." and a count-down from `retry_after_s`; 409
  `already_dead` / `too_few_healthy` and 404 `unknown_worker` → the sentences above. No request carries
  `X-Presenter-Passcode`; `POST /presenter/check` and `GET /batteries` are no longer called.
- **`GET /settings`**: `years` (year, label, description, real_until_utc) and each preset's `year`; `year` in
  `settings`, editable like the rest (`editable` lists it). Without `years` the site still works, reading each
  preset's year from its date.
- **Ticks**: `predicted: true` drives the badge, the shading and "projected"; missing means real.
- **`prediction_started`**: shown verbatim, with the AI icon, in both feeds.
- **`battery_status`**: 4-bit codes, battery i in byte i >> 1, low nibble for even i (tested); codes above 4 read as
  waiting; a short or malformed field falls back to "all waiting". Every frame's field is decoded, paused or not,
  so a backend that follows the freshness rule shows changes within a frame. After its own availability change
  while idle the site fetches `GET /state`, since idle messages carry no `battery_status`.
- **`race`**: read if it has a numeric `revenue_usd`; `null` or missing hides it. `potential` is read when it has a
  numeric `revenue_usd` and shown only while `shows_potential` is true; `shortfall_usd` is the fleet's loss,
  `potential.by_location[city].revenue_usd` minus the city's live revenue a city's. `race_update` events: verbatim,
  flag icon, coloured by `severity`.
- **`totals.by_location`, `availability.by_location`, `workers[].by_location`**: missing cities read as 0 (a dead
  controller has `{}`); a city's "On target" uses the tick's `error_pct` formula with its own target and actual and
  0.5% of its estimated nameplate.
- **`GET /results/ai-gains`**: `ai` and `baseline` pick the two strategies (default forecast and bands); only the
  `is_pitch` year is read; `share_of_best` and `surplus_mwh` as documented; the `spike_*` fields are no longer
  shown; 404 `no_results` hides the section.
- **`POST /run/start` 503 `simulator_restarting`**, **`POST /backtest/{job_id}/cancel`** and everything else as
  before: settings item shapes, the idle message and `last_run`, `run_finished`, 404 `no_results`, `skipped`
  reasons, amounts as fleet totals, `error_pct` as a percentage.

## Open issues

- Built and checked against docs/API.md and the mock only (years, prediction, potential, public crash, paused
  freshness); not run against the backend being built in parallel.
- Before the page was opened the map can show each city's action only (no per-battery past exists anywhere), and
  the corners' controllers are rebuilt from the kept events (the last 50): an older crash's hand-over may be
  missing. Availability and the race before the page was opened are unknown: the race hides, and the battery counts
  follow the city-level map (every battery counted online, doing its city's action).
- The event-triggered crash look relies on the client's replay of ownership. If a viewer joins after more than 50
  events, earlier hand-overs are unknown and a later crash of an adopter shows only its original block.
- The recorder is per tab and per visit: reloading the page, or a long stay on Results, starts a new recording
  (the gap is marked; the corners still work from history).
- `prefers-reduced-motion` could not be emulated in the browser pane; that path is covered by unit tests and by
  reading the renderer (no loop, no pulses, instant zoom).
- The browser pane sometimes reports the page as hidden, and then no ResizeObserver or animation frame fires;
  measurements taken then are wrong, so the numbers above come from when it was visible.

## Requests for integrator

1. **docs/UX.md**, to match what was built (2026-09-26):
   - Principle 6 (colours): green = selling, money and primary actions; **blue = charging** (the old "a ring
     while charging" is superseded), a lighter blue for neutral information and focus; violet = the AI's forecast
     and prediction; red = outages, failures and the Texas outline.
   - "Show charts", price card: "shaded teal while charging and amber while selling" → blue while charging, green
     while selling, as on the map; a violet wash over the AI's predicted prices.
   - The old "Live map" code table (charging "green with a thin light ring") and the availability drawer's "Find a
     battery" line: mark them superseded by "Live map v2" or remove them.
   - Availability drawer: opens on the left, with a light backdrop, so the dots stay visible.
   - Race corner: compact third bar "All online" with "projected · Outages cost $790 so far"; the full label
     "All batteries online (projected)" in the money card.
   - Crashing: the tile popover "Crash controller 7?" with "Crash it" / "Cancel"; buttons count down "Next crash
     in 2 s".
   - Zoom: the + / − buttons sit in the scope pill next to "All of Texas"; the + and − keys work too.
   - Results, "What the AI adds": three cards (Earns more, Gets closer to perfect, Soaks up surplus power), the
     pitch year (2025) only, no year switch, no 2024 note, no "Ready for spikes".
2. **Presenter mode leftovers outside the website**: `mk/fleet.mk` line 50 ("Presenter mode needs
   PRESENTER_PASSCODE…"), DEMO.md steps 1 and "Click Presenter mode…" (crashing is now public: just click
   "Crash a controller" or a controller tile), and a RUNLOG.md entry saying presenter mode is gone.
3. **Backend**:
   - `battery_status` within one frame while paused, for availability changes and crashes (docs/API.md); the
     site shows whatever each frame carries.
   - 2026 presets in `GET /settings`, each with `year`, including "From January 1" for both years: the site
     offers only years that have presets, so without them there is no 2026 on Start. (`make web-prices` skips
     non-2025 presets, since the snapshot is 2025; the mock has its own `predicted_2026` and `jan1_2026`.)
   - `years[].description` for 2026 should stay one short line; the site shows it as is under the Year setting.
   - `prediction_started`: the site shows the message verbatim; docs/API.md's wording is right.
   - `race.potential.label` is not shown (the site uses docs/UX.md's "All batteries online (projected)"); keep
     `shortfall_usd` and `potential.by_location` in sync with `by_location`.
   - The kill 429 should carry `retry_after_s` (the site counts down from it, else 2 s).
   - `GET /batteries` is no longer used by the site; keep it for API users or drop it.
4. **Still open from before** (not verified): after `POST /run/speed`, `snapshot.speed_tps` and
   `settings.speed_tps` should both show the new speed; if the model switch moves to TimesFM 2.5, send the new
   strategy label in `GET /settings` and results, and the site follows. `uplift.pct` stays a fraction (0.544);
   if the gains may ever cover a fleet other than `GET /results/pitch`'s, add `fleet_size` to AiGains.
