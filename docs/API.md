# fleet-sim API contract (v1)

The backend (FastAPI, `src/api/main.py`) and the website (`web/`) both build against this file.
Change it only through the integrator.

- **Base URL:** local `http://localhost:8000`; cloud is the Cloud Run URL. The website reads it from `VITE_API_BASE_URL`.
- **Formats:** JSON bodies. Times are ISO-8601. `*_utc` fields are UTC with a `Z`; `*_local` fields are America/Chicago with an offset.
- **Units:** power in kW (the UI may show MW), energy in kWh, prices in $/MWh, money in USD.
- **Signs:** power is **+ when selling to the grid** and **− when buying/charging**. Revenue is + when earned.
- **Naming:** the API says **worker**, which is the spec's term. The UI labels a worker a **"controller"**. Battery ids look like `LZ_HOUSTON-000123`.
- **CORS:** origins come from env `CORS_ORIGINS` (comma-separated) plus a regex for `https://*.vercel.app` and `http://localhost:5173`.
- **Errors:** always `{"error": "<code>", "message": "<plain-language sentence>"}` with a 4xx/5xx status.

## Settings

```json
{
  "preset": "spike_evening",
  "start_utc": "2025-05-16T21:00:00Z",
  "speed_tps": 2,
  "strategy": "forecast",
  "start_charge_level": 0.9,
  "fleet_size": 20000,
  "power_kw_range": [5.0, 11.5],
  "capacity_kwh_range": [10.0, 20.0],
  "locations": {"LZ_HOUSTON": 0.5, "LZ_NORTH": 0.5}
}
```
- `speed_tps` is 5-minute ticks per wall-clock second. Allowed values: 1, 2, 5, 10.
- `strategy` is one of `bands`, `forecast` or `peak`.
- Only `preset`, `speed_tps`, `strategy` and `start_charge_level` can be changed by visitors. The server ignores other keys.

## REST endpoints

| Method + path | Who | Body / query | Response |
|---|---|---|---|
| `GET /health` | anyone | – | `{"ok": true, "version": "…", "status": "idle\|starting\|running\|paused\|finished"}` |
| `GET /settings` | anyone | – | `{"defaults": Settings, "editable": [...], "presets": [Preset], "strategies": [Choice], "speeds": [Choice]}` |
| `GET /state` | anyone | – | the same object as the WebSocket `snapshot` message |
| `POST /run/start` | anyone | partial Settings | `202 {"run_id": "…", "settings": Settings}`. Starts a fresh shared run, replacing any current one. |
| `POST /run/pause` / `POST /run/resume` | anyone | – | `{"status": "paused" \| "running"}` |
| `POST /run/speed` | anyone | `{"speed_tps": 5}` | `{"speed_tps": 5}` |
| `POST /run/reset` | anyone | – | `{"status": "idle"}`. Stops the run, restarts all 20 workers (**including killed ones**) and brings every battery back online. |
| `POST /availability` | anyone | see below | `{"applied_from_tick_utc": "…", "affected": 6000, "available": 14000, "total": 20000}` |
| `GET /batteries/{battery_id}` | anyone | – | `{"battery_id", "location", "worker_id", "capacity_kwh", "power_kw", "charge_level", "power_kw_now", "available"}` |
| `POST /presenter/check` | anyone | header `X-Presenter-Passcode` | `200 {"ok": true}` or `403` |
| `POST /chaos/kill` | **presenter only** | header `X-Presenter-Passcode`; optional `?worker_id=7` (default: a random healthy worker) | `200 {"worker_id": 7, "pid": 1234, "killed_at_utc": "…"}`. See the error codes below. |
| `POST /backtest` | anyone | `{"strategies": ["bands","forecast","peak"], "year": 2025}` | `202 {"job_id": "…"}` |
| `GET /backtest/{job_id}` | anyone | – | `{"status": "running\|done\|failed", "run_ids": [...], "error": null}` |
| `GET /results/pitch` | anyone | – | Results (below) |
| `GET /forecast` | anyone | `?location=LZ_HOUSTON&at_utc=…` | Forecast (below): the latest forecast issued at or before `at_utc` |

`POST /chaos/kill` errors:

| Status | Code | Meaning |
|---|---|---|
| 403 | `forbidden` | Missing or wrong passcode |
| 503 | `presenter_passcode_not_set` | No passcode is configured yet |
| 409 | `already_dead` | That worker is already dead |
| 409 | `too_few_healthy` | The kill would leave fewer than 2 healthy workers |

**Rate limits** (global, because everyone shares one fleet):
- `/run/start` and `/run/reset`: one call per 5 s.
- `/availability`: 10 calls per second.
- Wrong passcodes: 5 per minute per IP.
- A limited call returns `429 {"error": "rate_limited", "retry_after_s": n}`.

**`POST /availability` bodies** take effect from the next tick:
- `{"scope": "location", "location": "LZ_HOUSTON", "offline_fraction": 0.3}` takes a seeded-random 30% of that area offline (the same fraction always picks the same batteries). `0` brings them all back.
- `{"scope": "worker", "worker_id": 7, "available": false}` takes all batteries currently owned by worker 7 offline **without** killing the process.
- `{"scope": "battery", "battery_id": "LZ_HOUSTON-000123", "available": false}`
- `{"scope": "all", "available": true}` brings everything back online.

## WebSocket `/live`

On connect, the server sends one `snapshot`. After that it sends a `frame` every 1 s, and an `idle` frame when no run is active. Clients reconnect automatically: Cloud Run closes connections after 60 min.

```json
{"type": "snapshot", "run_id": "…", "status": "running", "settings": Settings,
 "history": [Tick], "totals": Totals, "workers": [Worker], "failed_commands": FailedCommands,
 "availability": Availability, "events": [Event], "forecast": {"LZ_HOUSTON": Forecast, "LZ_NORTH": Forecast}}

{"type": "frame", "run_id": "…", "status": "running", "sim_time_utc": "…", "speed_tps": 2,
 "ticks": [Tick], "totals": Totals, "workers": [Worker], "failed_commands": FailedCommands,
 "availability": Availability, "events": [Event], "forecast": {…}}
```
- `snapshot.history` holds the last ≤ 2,000 ticks of the current run.
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
{"revenue_usd": 1234.5, "sold_kwh": 0, "bought_kwh": 0, "ticks": 123}
```

**Worker**
```json
{"worker_id": 7, "status": "healthy", "pid": 1234, "batteries": 1000, "last_tick_utc": "…", "adopted_from": [3]}
```
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

**Event**
```json
{"id": "…", "wall_utc": "…", "sim_utc": "…", "kind": "…", "severity": "info", "message": "…", "detail": {}}
```
- `kind` is one of: `run_started`, `run_paused`, `run_resumed`, `speed_changed`, `run_reset`, `worker_suspect`, `worker_dead`, `worker_adopted`, `availability_changed`, `price_spike`, `action_changed`, `forecast_issued`, `backtest_done`.
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
