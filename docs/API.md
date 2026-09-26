# fleet-sim API contract (v1)

The backend (FastAPI, `src/api/main.py`) and the website (`web/`) both build against this file.
Change it only through the integrator.

- **Base URL:** local `http://localhost:8000`; cloud is the Cloud Run URL. The website reads it from `VITE_API_BASE_URL`.
- **Formats:** JSON bodies. Times are ISO-8601. `*_utc` fields are UTC with a `Z`; `*_local` fields are America/Chicago with an offset.
- **Units:** power in kW (the UI may show MW), energy in kWh, prices in $/MWh, money in USD.
- **Signs:** power is **+ when selling to the grid** and **− when buying/charging**. Revenue is + when earned.
- **Naming:** the API says **worker**, which is the spec's term. The UI labels a worker a **"controller"**.
  - `worker_id` is **0–19**. Humans always see **"Controller N" with N = worker_id + 1**, in UI tiles and event messages alike.
  - Battery ids are fleet-wide: **`b00000`–`b19999`**, split into contiguous blocks in config order by `src.sim.fleet.assign_locations` (the one place that split is made). The fleet is the **Texas Triangle**, weighted by metro population:
    - Houston (`LZ_HOUSTON`): 7,000, b00000–b06999
    - Dallas–Fort Worth (`LZ_NORTH`): 8,000, b07000–b14999
    - San Antonio (`LZ_CPS`): 2,600, b15000–b17599
    - Austin (`LZ_AEN`): 2,400, b17600–b19999
  - City names shown to people come from `GET /fleet/layout` `cities[].name`.
- **Amounts:** `top_days[].usd`, `per_location_usd` and `revenue_usd` are **fleet totals** (all batteries), not per battery. `error_pct` is already a percentage: `0.02` means 0.02%.
- **CORS:** origins come from env `CORS_ORIGINS` (comma-separated) plus a regex for `https://*.vercel.app` and `http://localhost:5173`. Allowed request headers: `Content-Type` and `X-Presenter-Passcode`. Exposed response header: `Retry-After`.
- **Errors:** always `{"error": "<code>", "message": "<plain-language sentence>"}` with a 4xx/5xx status. A 429 also carries `retry_after_s` in the body and a `Retry-After` header.

## Settings

```json
{
  "preset": "ai_best_evening",
  "start_utc": "2025-05-30T23:00:00Z",
  "speed_tps": 2,
  "strategy": "forecast",
  "start_charge_level": 0.9,
  "fleet_size": 20000,
  "power_kw_range": [5.0, 11.5],
  "capacity_kwh_range": [10.0, 20.0],
  "locations": {"LZ_HOUSTON": 0.35, "LZ_NORTH": 0.40, "LZ_CPS": 0.13, "LZ_AEN": 0.12}
}
```
- `speed_tps` is 5-minute ticks per wall-clock second. Allowed values: 1, 2, 5, 10.
- `strategy` is one of `bands`, `forecast` or `peak`.
- Only `preset`, `speed_tps`, `strategy` and `start_charge_level` can be changed by visitors. The server ignores other keys.

## REST endpoints

| Method + path | Who | Body / query | Response |
|---|---|---|---|
| `GET /health` | anyone | – | `{"ok": true, "version": "…", "status": "idle\|starting\|running\|paused\|finished"}` |
| `GET /settings` | anyone | – | `{"defaults": Settings, "editable": [...], "presets": [Preset], "strategies": [Strategy], "speeds": [Speed]}` (shapes below the table) |
| `GET /state` | anyone | – | the same object as the WebSocket `snapshot` message |
| `POST /run/start` | anyone | partial Settings | `202 {"run_id": "…", "settings": Settings}`. Starts a fresh shared run, replacing any current one. |
| `POST /run/pause` / `POST /run/resume` | anyone | – | `{"status": "paused" \| "running"}` |
| `POST /run/speed` | anyone | `{"speed_tps": 5}` | `{"speed_tps": 5}`. The next snapshot/frame `speed_tps` **and** `settings.speed_tps` both show the new speed. |
| `POST /run/reset` | anyone | – | `{"status": "idle"}`. Stops the run, restarts all 20 workers (**including killed ones**) and brings every battery back online. |
| `POST /availability` | anyone | see below | `{"applied_from_tick_utc": "…", "affected": 6000, "available": 14000, "total": 20000}` |
| `GET /batteries/{battery_id}` | anyone | – | `{"battery_id", "location", "city", "lonlat", "worker_id", "capacity_kwh", "power_kw", "charge_level", "power_kw_now", "available"}` |
| `GET /fleet/layout` | anyone | – | Layout (below): where every battery sits, for the map. It is static per fleet, so cache it by `version`. |
| `POST /presenter/check` | anyone | header `X-Presenter-Passcode` | `200 {"ok": true}` or `403` |
| `POST /chaos/kill` | **presenter only** | header `X-Presenter-Passcode`; optional `?worker_id=7` (default: a random healthy worker) | `200 {"worker_id": 7, "pid": 1234, "killed_at_utc": "…"}`. See the error codes below. |
| `POST /backtest` | anyone | `{"strategies": ["bands","forecast","peak"], "year": 2025}` | `202 {"job_id": "…"}`. While a job runs, this returns that job's id. Bad input gives 400. |
| `GET /backtest/{job_id}` | anyone | – | `{"status": "running\|done\|failed\|cancelled", "run_ids": [...], "skipped": {"forecast": "why"}, "error": null}`. Unknown id gives 404. |
| `POST /backtest/{job_id}/cancel` | anyone | – | Stops a running full-year job: `200 {"status": "cancelled"}`. Nothing is stored, and the previous results stay. `409 not_running` ("That full-year run has already finished.") if the job has already finished; `404 unknown_job` if the id is unknown. A cancelled job emits a `backtest_done` event whose message says it was stopped. |
| `GET /results/pitch` | anyone | – | Results (below). Before any full-year run: `404 {"error": "no_results", "message": "No full-year results yet."}` |
| `GET /results/ai-gains` | anyone | – | AiGains (below): what the AI forecast adds over the simple rule, per year (the pitch year first, then other complete years). `404 no_results` if none. |
| `GET /forecast` | anyone | `?location=LZ_HOUSTON&at_utc=…` (`location=ERCOT&series=demand` for demand) | Forecast (below): the latest forecast issued at or before `at_utc`, or 404 `no_forecast` |

`POST /chaos/kill` errors:

| Status | Code | Meaning |
|---|---|---|
| 403 | `forbidden` | Missing or wrong passcode |
| 503 | `presenter_passcode_not_set` | No passcode is configured yet |
| 409 | `already_dead` | That worker is already dead |
| 409 | `too_few_healthy` | The kill would leave fewer than 2 healthy workers |

**Other error codes:**

| Code | Meaning |
|---|---|
| `no_active_run` | Pause, resume or speed was called with no run |
| `simulator_unavailable` | The coordinator isn't up yet (cold start) |
| `unknown_worker` | worker_id outside 0–19 |
| `unknown_battery` | No battery with that id |
| `not_a_worker` | The pid is not a registered worker, so it is never killed |
| `invalid_*` | A malformed field, e.g. `invalid_speed` |
| `simulator_restarting` | 503 from `/run/start` when the controllers aren't back within 15 s of a Reset. Carries `retry_after_s` and a `Retry-After` header, like a 429. |

**`GET /settings` item shapes:**
- Preset: `{"id", "label", "date_local", "start_utc", "start_local", "description"}`
- Strategy: `{"id": "bands", "label": "Simple rule", "description": "…"}`
- Speed: `{"tps": 2, "label": "1 hour every 6 s"}`

**Rate limits** (global, because everyone shares one fleet):
- `/run/start` and `/run/reset`: one call per 5 s.
- `/availability`: 10 calls per second.
- Wrong passcodes: 5 per minute per IP.
- A limited call returns `429 {"error": "rate_limited", "retry_after_s": n}`.

**`POST /availability` bodies** take effect from the next tick:
- `{"scope": "location", "location": "LZ_HOUSTON", "offline_fraction": 0.3}` takes a seeded-random 30% of that area offline (the same fraction always picks the same batteries). `0` brings them all back.
- `{"scope": "worker", "worker_id": 7, "available": false}` takes all batteries currently owned by worker 7 offline **without** killing the process.
- `{"scope": "battery", "battery_id": "b00123", "available": false}`
- `{"scope": "all", "available": true}` brings everything back online.

## WebSocket `/live`

On connect, the server sends one `snapshot`. After that it sends a `frame` every 1 s while a run exists, and an `idle` message every 5 s when none does:
`{"type": "idle", "status": "idle", "run_id": null, "last_run": {"run_id": "…", "totals": Totals} | null}`. Clients reconnect automatically: Cloud Run closes connections after 60 min.

```json
{"type": "snapshot", "run_id": "…", "status": "running", "sim_time_utc": "…", "speed_tps": 2, "settings": Settings,
 "history": [Tick], "totals": Totals, "workers": [Worker], "failed_commands": FailedCommands,
 "availability": Availability, "events": [Event], "forecast": {"<location>": Forecast},
 "battery_status": "<base64>", "race": Race}

{"type": "frame", "run_id": "…", "status": "running", "sim_time_utc": "…", "speed_tps": 2,
 "ticks": [Tick], "totals": Totals, "workers": [Worker], "failed_commands": FailedCommands,
 "availability": Availability, "events": [Event], "forecast": {…}, "battery_status": "<base64>", "race": Race}
```
- `snapshot.history` holds the last ≤ 2,000 ticks of the current run.
- With no run, `snapshot.settings` is `null` and `run_id` is `null`, and `history` is empty.
- `frame.ticks` holds every tick since the previous frame. Ticks are never sampled.
- `frame.events` holds only new events.
- `frame.forecast` is present only when a new forecast was issued.

**Tick**
```json
{"ts_utc": "2025-05-16T21:05:00Z", "ts_local": "2025-05-16T16:05:00-05:00",
 "price": {"LZ_HOUSTON": 42.1, "LZ_NORTH": 39.8},
 "decision_price": {"LZ_HOUSTON": 40.2, "LZ_NORTH": 38.9},
 "action": {"LZ_HOUSTON": "charge", "LZ_NORTH": "hold"},
 "target_kw": {"LZ_HOUSTON": -48000.0, "LZ_NORTH": 0.0, "fleet": -48000.0},
 "actual_kw": {"LZ_HOUSTON": -47990.2, "LZ_NORTH": 0.0, "fleet": -47990.2},
 "error_pct": 0.02,
 "revenue_usd": {"LZ_HOUSTON": -1.61, "LZ_NORTH": 0.0, "fleet": -1.61},
 "charge_level": {"LZ_HOUSTON": 0.62, "LZ_NORTH": 0.55, "fleet": 0.585},
 "healthy_workers": 20}
```
- `decision_price` is the last *published* price the strategy saw (the 15-min lag).
- `action` is `charge`, `sell` or `hold`.
- `target_kw` is the feasible fleet target, not the raw request.
- `error_pct` is `|actual − target| / max(|target|, 0.5% of nameplate)`.

**Totals**
```json
{"revenue_usd": 1234.5, "sold_kwh": 0, "bought_kwh": 0, "ticks": 123,
 "by_location": {"LZ_HOUSTON": {"revenue_usd": 0, "sold_kwh": 0, "bought_kwh": 0}}}
```

**Worker**
```json
{"worker_id": 7, "status": "healthy", "pid": 1234, "batteries": 1000, "last_tick_utc": "…", "adopted_from": [3],
 "by_location": {"LZ_NORTH": 1000}}
```
`by_location` lists only the cities this controller currently holds. It is `{}` for a dead controller, and changes after hand-overs. `totals.by_location` always has every city.

`status` is `starting`, `healthy`, `suspect` or `dead`.

**FailedCommands**
```json
{"count": 12, "recent": [{"battery_id": "…", "worker_id": 7, "reason": "…", "tick_utc": "…"}]}
```
`recent` holds up to 10 entries. It mirrors the `dead_letter` stream.

**Availability**
```json
{"available": 19800, "total": 20000,
 "by_location": {"LZ_HOUSTON": {"available": 9900, "total": 10000}},
 "offline_workers": [7]}
```
`offline_workers` lists workers whose batteries were set offline via `scope=worker`.

**battery_status** (in `snapshot` and every `frame`): the state of every battery at the latest tick.
- Format: base64 of packed 4-bit codes. Battery *i* (`b` + zero-padded *i*) is in byte *i*/2 (integer division): the low nibble for even *i*, the high nibble for odd *i*. 20,000 batteries take 10,000 bytes.
- Codes:

  | Code | State |
  |---|---|
  | 0 | offline (taken offline by a visitor) |
  | 1 | waiting (online, 0 kW) |
  | 2 | selling |
  | 3 | charging |
  | 4 | controller down: its controller is suspect or dead, and the battery hasn't been handed over yet |

- With no run, every battery is 1 (online batteries) or 0 (offline). `idle` messages carry no `battery_status`, so after an availability change while idle, clients refetch `GET /state`.
- Code 4 lasts about 2 ticks: the missed tick and the hand-over tick. At 5–10 ticks/s a 1 s frame can miss it, so clients should also flash a crashed controller's batteries on `worker_suspect`, `worker_dead` and `worker_adopted` events.

**Race** (in `snapshot` and every `frame` while a run exists): a shadow copy of the fleet running the comparison strategy side by side with the live one.
- The comparison strategy is `bands` ("Simple rule"), or `forecast` when the live run itself uses `bands`.
- It starts from the same batteries and starting charge, sees the same prices at the same decision lag, and mirrors the same availability overrides.
- It does not mirror controller crashes, because it has no controllers.
- It uses the same physics and allocation as the backtest.
```json
{"strategy": "bands", "label": "Simple rule",
 "revenue_usd": 31020.5, "by_location": {"LZ_HOUSTON": {"revenue_usd": 0, "charge_level": 0.41, "action": "sell"}},
 "charge_level": 0.43, "live_minus_race_usd": 15290.1}
```
- `live_minus_race_usd` is the live fleet's `totals.revenue_usd` minus the race's. Positive means the live strategy is ahead.
- `race` is `null` while idle, while a run is starting, and when there is nothing to race: a live simple rule with no forecasts in any city.
- The after-spike `race_update` has severity `good` when the live strategy leads, and `info` otherwise.

**AiGains** (`GET /results/ai-gains`)
```json
{"ai": "forecast", "baseline": "bands",
 "years": [{"year": 2025, "is_pitch": true, "fleet_size": 20000, "best_possible_per_battery_usd_year": 421.29,
   "strategies": {"forecast": StrategyGains, "bands": StrategyGains, "peak": StrategyGains},
   "uplift": {"per_battery_usd_year": 67.28, "pct": 0.544, "fleet_usd_year": 1345600}}]}
```
StrategyGains is:
```json
{"label": "AI forecast (TimesFM-3)", "per_battery_usd_year": 190.95, "fleet_usd_year": 3819000, "share_of_best": 0.453,
 "surplus_mwh": 7011.3, "spike_mwh": 25.2, "spike_events": 21, "spike_charge_level": 0.231,
 "sold_mwh": 80168.1, "bought_mwh": 89231.5, "avg_sell_usd_mwh": 66.58, "avg_buy_usd_mwh": 17.02}
```
- `surplus_mwh`: energy bought at prices ≤ $0/MWh.
- Spikes are 5-minute prices ≥ $1,000/MWh:
  - `spike_mwh` is the energy sold during them.
  - `spike_events` is the number of separate spikes.
  - `spike_charge_level` is the fleet's charge level when each spike began, averaged.
- `share_of_best` is per-battery earnings ÷ the same year's hindsight best.
- **Fields that can be missing:**
  - The insight fields, on runs stored before they existed.
  - `best_possible_per_battery_usd_year` and `share_of_best`, when that year has no hindsight run.
  - The AI entry and `uplift`, when the year has no AI run.
- `spike_charge_level` is `null` when the year had no spikes.

**Layout** (`GET /fleet/layout`)
```json
{"version": "…", "fleet_size": 20000,
 "cities": [{"location": "LZ_NORTH", "name": "Dallas–Fort Worth", "short": "DFW", "batteries": 8000,
             "first_battery": 7000, "center_lonlat": [-96.97, 32.82]}],
 "positions": "<base64>"}
```
- `positions` is a little-endian Int16Array `[lon0, lat0, lon1, lat1, …]`, one pair per battery in id order. `lon = -100 + v/1000` and `lat = 31 + v/1000`, so the resolution is about 100 m.
- Positions are simulated: they are seeded, and inside real metro areas. There are no real addresses.
- The response carries an `ETag`. Send `If-None-Match` to get a `304` when nothing changed.
- The map projects lon/lat with Texas Centric Albers Equal Area (EPSG:3083: standard parallels 27.5°N and 35°N, origin 18°N, 100°W). The web bundles the Texas outline in the same projection.

**Event**
```json
{"id": "…", "wall_utc": "…", "sim_utc": "…", "kind": "…", "severity": "info", "message": "…", "detail": {}}
```
- `kind` is one of: `run_started`, `run_paused`, `run_resumed`, `run_finished`, `speed_changed`, `run_reset`, `worker_suspect`, `worker_dead`, `worker_adopted`, `availability_changed`, `price_spike`, `action_changed`, `forecast_issued`, `backtest_done`, `race_update`.
  - `race_update` is a plain-language comparison with the race fleet. A "spike" here is any city's price reaching `api.race_spike_usd_mwh` ($300/MWh), which is lower than the $1,000 `price_spike` alert. It comes at the start of each price spike (e.g. "Prices jumped to $1,757 in all four cities. The AI fleet had 54% charge left; the simple rule had 21%.") and after it ends ("The AI is now $12,400 ahead of the simple rule.").
  - A run finishes when the replay reaches the end of the loaded data.
- Worker events carry `detail.worker_id` (0-based) and `detail.controller` (1-based, as shown to people). `worker_adopted` also carries `detail.adopted_by` (worker ids) and `detail.ms`.
- `severity` is `info`, `good` or `warn`.
- `message` is ONE plain-language sentence that the UI shows verbatim (see docs/UX.md for tone), for example *"Controller 7 stopped responding. Its 1,000 batteries were handed to the other 19 in 2 ms."* Technical values go in `detail`.

**Forecast**
```json
{"location": "LZ_HOUSTON", "series": "rt_price", "model": "timesfm-3.0", "issued_utc": "…", "step_minutes": 15,
 "points": [{"ts_utc": "…", "p10": 0, "p50": 0, "p90": 0}]}
```

**Results** (`GET /results/pitch`)
```json
{"year": 2025, "generated_utc": "…", "fleet_size": 20000, "fleet_power_mw": 165.1, "fleet_energy_mwh": 300.2,
 "assumptions": ["Gross wholesale energy value at ERCOT real-time prices (15-minute settlement)", "…"],
 "strategies": [{"strategy": "forecast", "label": "AI forecast (TimesFM-3)", "run_id": "…", "is_headline": true,
                 "per_battery_usd_year": 0, "fleet_usd_year": 0, "usd_per_kwh_year": 0, "usd_per_kw_year": 0,
                 "cycles_per_year": 0, "per_location_usd": {"LZ_HOUSTON": 0, "LZ_NORTH": 0},
                 "top_days": [{"date_local": "2025-05-16", "usd": 0}]}],
 "ceiling": {"label": "Best possible (perfect hindsight)", "per_battery_usd_year": 0, "fleet_usd_year": 0}}
```
