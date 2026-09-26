# Website UX guide

The human's brief: *"as user friendly as possible, no unnecessary jargon, technical enough, but extremely easy to use."*

## Principles
1. **Plain words first.** Every number has a unit and a human translation, e.g. "$120/MWh (12¢ per kWh)".
2. **One obvious next action per screen.** Secondary actions are visually quieter.
3. **Every panel explains itself.** Each has a one-line *What is this?* under its title and an optional "How it works" expander of at most 3 sentences.
4. **Technical on demand.** A global **"Show technical details"** switch (off by default) reveals:
   - raw values: kW per controller, process IDs, tick numbers, error %, message counts
   - spec terms in brackets, e.g. "Controllers (worker processes)"
5. **Explain acronyms or hide them.**
   - "ERCOT" is first introduced as "ERCOT, the Texas grid operator".
   - Load zones are shown as cities: LZ_HOUSTON "Houston", LZ_NORTH "Dallas–Fort Worth", LZ_CPS "San Antonio", LZ_AEN "Austin". Names come from `GET /fleet/layout`.
6. **Color has meaning, and color is never the only signal** (also use icon or label):
   - **green** = selling, money and primary actions
   - **blue** = charging
   - **violet** = the AI forecast and its prediction
   - **grey** = waiting or offline
   - **blue** = neutral information and keyboard focus
   - **red** = outages (offline or crashed batteries), failures and the Texas outline
7. **Desktop-first**, with readable text at 1280px. Keyboard-accessible, visible focus and AA contrast. **One always-dark theme site-wide**, matching the live map: dark gray page, translucent dark panels, green primary actions. Red is only for failures and the Texas outline.
8. **Honest labels.**
   - Replays say "Replaying real prices from May 16, 2025".
   - Money is "gross wholesale value".
   - Recorded or estimated values are always labeled.

## Glossary (UI word ← internal term)
| UI says | Internal |
|---|---|
| Controller | worker process |
| Charge level | SoC (state of charge) |
| Power the fleet was asked for / Power delivered | target kW / actual kW |
| Selling / Charging / Waiting | discharge / charge / hold |
| Spare capacity | headroom |
| Failed commands | dead-letter entries |
| Full-year results | backtest |
| 5-minute step | tick |
| Simple rule | Strategy v1 (percentile bands) |
| AI forecast (TimesFM-3) | forecast strategy |
| Evening peak | peak strategy |
| Price right now | RT settlement point price |
| Houston / Dallas–Fort Worth / San Antonio / Austin | LZ_HOUSTON / LZ_NORTH / LZ_CPS / LZ_AEN |

## Screens

### 1. Start (landing)
- **Headline:** "What if 20,000 home batteries traded on the Texas power grid?"
- **Subhead:** "Replay real 2025 electricity prices and watch a fleet of home batteries buy cheap, sell high, and keep working when parts of the system crash."
- **Run settings card**, placed ABOVE the button:
  - 20,000 home batteries
  - 5–11.5 kW, 10–20 kWh each
  - The Texas Triangle: Dallas–Fort Worth, Houston, San Antonio and Austin
  - Replay: {preset label, e.g. "Price-spike evening, May 16, 2025, from 4 pm"}
  - Speed: "1 hour of market time every 6 seconds"
  - Decision method: "AI forecast (TimesFM-3)"
  - Batteries start 90% charged
- A "Change settings" link opens a small editor. Only preset, speed, decision method and starting charge are editable.
- **Primary button:** "Start the simulation".
- **While the backend wakes up** (Cloud Run cold start), show "Waking up the simulator… (usually a few seconds)".
- **If a run is already going:** "A simulation is running. Watch live" plus a secondary "Start over".

### 2. Live
- **Top bar:** replay clock ("Jul 11, 2025, 8:45 pm, Central time"), Pause/Resume, Speed, Reset, and **Crash a controller**, open to everyone.
- **The race** sits in the map's money corner, under the money: "AI forecast ahead by $15,290" over two bars (the live strategy vs the simple-rule shadow fleet, from `race`). It switches to the selected city's numbers, and `race_update` events show in the feed with a flag icon.
- **Price card** ("Electricity price right now"):
  - Real price line per area, shaded teal while charging and amber while selling.
  - The AI forecast as a soft band (10–90% range) when available.
  - Explainer: "Real prices from ERCOT, the Texas grid operator. Batteries buy when power is cheap and sell when it's expensive."
- **Power card** ("Power: asked for vs delivered"):
  - Dashed = plan, solid = delivered.
  - Explainer: "The two lines should overlap. When a controller crashes, watch the gap close within a few seconds."
- **Controllers card:** a grid of 20 tiles, green = working, amber = not responding, red = crashed.
  - Each tile shows "1,000 batteries".
  - Explainer: "Each controller runs 1,000 batteries. If one crashes, the others take over its batteries."
  - Also shows a "Failed commands: N" counter with a one-line explanation.
- **Money card:** a large running counter, "Earned so far in this replay: $X", with sold/bought kWh underneath.
- **Battery availability panel** (side drawer: "Take batteries offline"):
  - A slider per area ("% offline").
  - "Take a controller's batteries offline" (a controller picker).
  - "Find a battery" by ID.
  - Explainer: "Offline batteries can't charge or sell, so the fleet's plan shrinks. This is different from crashing a controller."
- **"What just happened" feed:** the plain-language `events[].message`, newest first, max 6 visible.

### 3. Results
- **Big number:** "$X per battery per year".
  - Subline: "$Y million a year for the 20,000-battery fleet".
  - Small print: "$/kWh-year", "cycles per year".
- **Comparison bars:** Simple rule vs AI forecast vs Evening peak vs Best possible (hindsight). Explainer: "Best possible assumes perfect knowledge of future prices. No real system can reach it."
- **Top 10 days by earnings:** a list or small bar chart.
- **"How we got this number"** (expander):
  - gross wholesale value at ERCOT 2025 real-time prices
  - excludes retail/delivery charges, grid-support services and battery wear
  - the fleet is too small to move prices
  - TimesFM-3 licence note: non-commercial research weights; a production version would use TimesFM 2.5
- **Button:** "Re-run the full year" (POST /backtest), showing progress. Next to it is a **Stop** button (POST /backtest/{id}/cancel). Stop is enabled only while a re-run is running; a stopped run keeps the previous results ("Stopped. The previous results are still shown.").
- **"What the AI adds"** is a full-width section right under the big number (`GET /results/ai-gains`):
  - **Headline:** "+$67 per battery per year (+54%) vs the simple rule, $1.35 million more a year for the 20,000-battery fleet", computed from `uplift`.
  - **Three cards**, each with bars and a "What this means" line:
    - *Earns more*
    - *Gets closer to perfect*: `share_of_best`
    - *Soaks up surplus power*: `surplus_mwh`
  - **Pitch year (2025) only.** The human removed the 2024 switch and, with it, the 2024-only "Ready for spikes" card.
  - No limitations line in this section (the human's call). The fine print stays in "How we got this number".
  - A 404 hides the section.

## Crashing controllers
- Open to everyone: there is no presenter mode and no passcode. Rate-limited to one crash per 2 s, and it never leaves fewer than 2 healthy controllers.
- **Reset** brings every controller back and every battery online.

## Live map (the default Live view, added 2026-09-26, per the human's direction)
- **Page.** Full screen with a **dark gray background** (`#25282c`), the same as every other page.
- **Texas.** A bright red outline only (stroke, no fill), drawn as large as fits while staying pretty (keep a small margin). New Mexico, Oklahoma, Mexico and the Gulf leave the 4 screen corners free; the corners hold the controls and numbers.
- **Batteries.** 20,000 green dots, from `GET /fleet/layout` positions (EPSG:3083 projection) and `battery_status` codes.
  - Batteries close together merge into one larger dot, with area proportional to count, so the map stays readable at any size.
  - Colours by code:

    | Code | Look |
    |---|---|
    | 1 waiting | green, medium brightness |
    | 2 selling | bright green with a soft glow |
    | 3 charging | green with a thin light ring |
    | 0 offline | dim gray |
    | 4 controller down | the dot flashes dark, then returns to green when it's handed over |

    That flash is the visible crash-recovery moment.
  - `prefers-reduced-motion` disables glows and pulses.
- **Cities.** Dallas–Fort Worth, Houston, San Antonio and Austin get labels next to their clusters. Clicking a city, or tabbing to it and pressing Enter, **selects** it:
  - The corners switch to that city's numbers.
  - Its dots are highlighted and the others dimmed a little.
  - An "All of Texas" chip, clicking empty map or pressing Esc goes back to the whole fleet.
- **Corners.** Compact translucent dark panels, with the plain language and units rules as elsewhere:
  - **Top-left:** replay clock; the selected city's price right now, with its ¢/kWh translation and a mini price line; its forecast band if present.
  - **Top-right:** money earned so far (city or fleet); power asked for vs delivered, as numbers plus a sparkline and an On target badge.
  - **Bottom-left:** Pause/Resume, Speed, Reset, Take batteries offline, and Crash a controller (public).
  - **Bottom-right:** 20 mini controller tiles (serving the selected city = highlighted, using `workers[].by_location`); batteries online/offline for the selection; the last 3 "What just happened" events.
- **"Show charts."** A button that opens the existing detailed Live layout; nothing is lost.
- **Honesty.** A small caption: "Battery locations are simulated inside real metro areas."


## Live map v2 (the human's feedback, 2026-09-26). This supersedes the earlier dot looks.
- **Dot colours: one meaning each, and shape as well as colour.** The legend in the bottom-right corner doubles as the battery counts.

  | Dot | Meaning |
  |---|---|
  | **Green solid** | selling power to the grid |
  | **Blue solid** | charging (buying) |
  | **Gray, small** | waiting |
  | **Red ring** (hollow) | offline, because a visitor took it offline |
  | **Red pulsing** | its controller crashed. It returns to its normal look once another controller takes it over. |

  - Offline and crashed batteries are drawn red at **their own locations**. That's the random subset for a city slider, or that controller's block of batteries.
  - Aggregated dots use the majority state. A cell with any offline or crashed batteries gets a red edge, so outages are never hidden inside a green dot.
- **Zoom: exactly one level.** The map loads showing all of Texas.
  - A **+ / −** control, or a double-click, zooms to the **Texas Triangle** (the bounding box of the four metros, with margin), centred on screen.
  - When zoomed in, dots re-aggregate at the new scale, so they are clearly visible (smaller cells, more dots).
  - The zoom animates smoothly; Esc zooms back out.
- **Timeline slider** in the top-left corner, compact: it must not inflate the corner.
  - The track is a mini price line for the run so far, with green bands where the fleet was selling and blue bands where it was buying, and small ▲/▼ markers at the highs and lows.
  - Dragging, or the ← → keys, shows the map and corners **as they were at that moment**, with a "Viewing 7:45 pm · Back to live" chip. The simulation keeps running.
  - Map dots for the past come from the frames this page has seen. Before the page was opened, the corners still work from history, and the map shows the city totals only.
- **Availability drawer** (opens on the LEFT, with a light backdrop, so the cities stay visible).
  - "Bring every battery back online" is at the **top** of the panel.
  - The "Find a battery" section is removed (superseding the earlier "Find a battery" line).
- **Crash a controller:** a public button, or click a controller tile for a "Crash controller 7?" popover. A countdown shows until the next crash is allowed.
- **Zoom:** the + / − buttons sit in the scope pill next to "All of Texas".
- **Race (compact):** the map corner shows an "All online" row plus one line, "projected · Outages cost $X so far".
- **Race.** When batteries or controllers went offline (`race.shows_potential`), a **third bar** appears: "All batteries online (projected)", from `race.potential`, together with the shortfall. So the race shows actual AI vs simple rule vs what the AI would have earned with everything online.
- **Year.** The Start settings get **Year: 2025 / 2026** (no 2024). For 2026, prices are real until `real_until_utc`, then the AI's prediction.
  - The timeline shades the predicted part.
  - The clock shows a "Predicted" badge on predicted ticks.
  - The `prediction_started` event appears in the feed.
  - Money on predicted ticks is labelled "projected".
- **Presenter mode** is removed from the header, and its dialog and session storage are removed too.
