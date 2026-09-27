# FLEET SIM — BUILD SPEC (for AI coding agent)

You are the build crew for a 48-hour solo hackathon project. The human is
architect, reviewer, and demo owner. Follow this spec exactly. Do not change
interface contracts without asking. Build milestone by milestone. Commit at
every green milestone.

## 1. PROJECT GOAL

Simulate a fleet of 10,000 home batteries buying/selling power against real
historical Texas (ERCOT) electricity prices, coordinated by a
coordinator/worker system that survives worker crashes, and report the money
the fleet would have earned.

Demo requirements (all three MUST work live):
1. Real ERCOT price data drives the sim (Track: Open Grid Data).
2. Kill workers live; fleet re-balances and holds its power target
   (Track: Orchestration).
3. Final screen shows fleet revenue in dollars computed from real data
   (Track: Most Commercializable).

## 2. HARD CONSTRAINTS

- Timeline: 48 hours, one human + you. Prefer boring, working code.
- Runs on one laptop. No cloud services.
- Stack (pinned): Python 3.12, FastAPI, PostgreSQL, Redis, React + Vite,
  WebSocket for live updates.
- Workers MUST be separate OS processes (killable with SIGKILL on stage),
  not threads.
- Future stages will swap: replay prices -> live prices; sim batteries ->
  real hardware APIs; rule strategy -> solver; pretend ledger -> real
  billing. Therefore the four interfaces in section 4 are FROZEN seams.
  Everything else may be refactored freely.

## 3. NON-GOALS (do not build)

- No auth, users, billing, multi-tenancy.
- No live/streaming price ingestion (replay only).
- No ML forecasting, no solver/optimizer.
- No real hardware integration.
- No mobile UI. Desktop browser only.

## 4. FROZEN INTERFACE CONTRACTS (Python)

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Protocol

@dataclass(frozen=True)
class PriceTick:
    ts: datetime            # UTC, 5-minute grid
    location: str           # e.g. "HB_NORTH"
    price_usd_mwh: float
    demand_mw: float | None
    fuel_mix: dict | None   # {"gas": .., "wind": .., ...} fractions

@dataclass
class BatteryState:
    battery_id: str
    capacity_kwh: float
    power_kw: float         # max charge/discharge rate
    soc: float              # 0.0-1.0 state of charge
    efficiency: float       # round-trip, e.g. 0.90
    available: bool

@dataclass(frozen=True)
class Command:
    command_id: str         # uuid; workers MUST dedupe on this
    battery_id: str
    setpoint_kw: float      # + = discharge to grid, - = charge
    tick_ts: datetime

class PriceFeed(Protocol):
    def ticks(self, start: datetime, end: datetime,
              location: str) -> Iterator[PriceTick]: ...

class BatteryAdapter(Protocol):
    def apply(self, cmd: Command) -> None: ...      # idempotent per command_id
    def read(self, battery_id: str) -> BatteryState: ...

class Strategy(Protocol):
    def target_kw(self, tick: PriceTick,
                  fleet_soc: float,
                  fleet_headroom_kw: float,
                  price_history: list[float]) -> float: ...

class Ledger(Protocol):
    def record(self, tick: PriceTick, delivered_kwh: float) -> None:
        """delivered_kwh: + = sold to grid, - = bought."""
    def summary(self) -> dict: ...   # totals, per-location, per-battery avg
```

Seam tests: write pytest contract tests for each Protocol FIRST. Any
implementation (replay or future live) must pass the same test suite.

## 5. DATA

### 5.1 Source (VERIFY AT HOUR 0 — flagged unverified)
- Try Python lib `gridstatus` for ERCOT pulls. If unsuitable, download
  ERCOT public market reports directly. Endpoints, history depth, and rate
  limits are NOT verified. First task: prove one day of real data lands in
  Postgres, then backfill.
- Target: 1 full year (2025) of real-time prices at 5-min or 15-min
  resolution, plus day-ahead hourly prices, for 2-3 hubs
  (e.g. HB_NORTH, HB_HOUSTON). Also system demand and fuel mix if cheap
  to get; else drop (nullable columns).
- If native resolution is 15-min, forward-fill to 5-min ticks and note it.

### 5.2 Postgres schema
```sql
CREATE TABLE prices (
  ts timestamptz NOT NULL,
  location text NOT NULL,
  market text NOT NULL CHECK (market IN ('RT','DA')),
  price_usd_mwh double precision NOT NULL,
  demand_mw double precision,
  fuel_mix jsonb,
  PRIMARY KEY (ts, location, market)
);

CREATE TABLE backtest_runs (
  run_id uuid PRIMARY KEY,
  started_at timestamptz,
  params jsonb,
  fleet_total_usd double precision,
  per_battery_avg_usd double precision,
  per_location jsonb,
  notes text
);
```

## 6. SIMULATION SPEC

- Tick = 5 minutes. Sim clock replays the historical year; support a
  speed multiplier (real-time for demo, max-speed for backtest).
- Battery defaults (all configurable in one config file):
  capacity_kwh=13.5, power_kw=5.0, efficiency=0.90 (ASSUMPTION — typical
  home battery class; human may adjust), initial soc=0.5.
- Physics per tick per battery:
  - energy = setpoint_kw * (5/60) hours, capped by power_kw and by SoC
    bounds [0.20, 1.00]  (0.20 = safety reserve floor).
  - Charging stores energy * sqrt(efficiency); discharging delivers
    energy * sqrt(efficiency) (split round-trip loss both ways).
- Fleet target allocation: coordinator splits fleet target_kw across
  batteries proportional to each battery's headroom in the commanded
  direction. Unavailable batteries get 0.

## 7. ORCHESTRATION SPEC

Topology: 1 coordinator process, 20 worker processes, 500 sim batteries
per worker = 10,000. If laptop cannot hold 10,000 at 5-min ticks
(load-test at hour 2), fall back to 2,000 (10 x 200) — flag to human.

Redis layout:
- `commands:{worker_id}` — Redis Stream; coordinator XADDs Command batches.
- `telemetry` — Redis Stream; workers XADD per-tick cohort summaries.
- `hb:{worker_id}` — key with 6s TTL; worker SETs every 2s (heartbeat =
  "I'm alive" ping).
- `dead_letter` — Stream; commands that failed 3 retries, with reason.

Rules (MUST):
- Idempotency: workers keep a seen-set of command_ids (Redis SET per
  worker, or in-proc + replay-safe); applying the same command twice has
  the effect of once.
- Failure detection: coordinator scans heartbeats each tick; a worker with
  expired key for >= 3 intervals (6s) is DEAD.
- Recovery: DEAD worker's battery cohort is reassigned across healthy
  workers within 1 tick; battery state lives in Redis (hash per battery)
  so any worker can adopt a cohort. Fleet output MUST return to within 5%
  of target within 3 ticks of a kill.
- Chaos endpoint: `POST /chaos/kill` SIGKILLs one random worker (or
  `?worker_id=`). Dashboard button calls it.

## 8. STRATEGY v1 (rule-based)

- Maintain trailing 7-day price history per location.
- If current RT price <= 25th percentile of trailing window: fleet target
  = max charge.
- If current RT price >= 90th percentile: fleet target = max discharge.
- Else: target 0 (hold).
- Respect SoC floor 0.20 always.
- Ship a second trivial Strategy (discharge only 18:00-21:00 local) to
  prove the seam swaps via config.

## 9. BACKTEST + LEDGER

- Replay full year at max speed. Ledger prices energy at the RT price of
  the tick when it was delivered/bought.
- Outputs (persist to backtest_runs + JSON export): fleet total USD,
  avg USD per battery per year, per-location totals, top-10 spike days by
  revenue.
- MUST finish a full-year, full-fleet backtest in < 10 minutes on laptop
  (drop to 2,000 batteries for backtest if needed; scale revenue is
  per-battery anyway).

## 10. DASHBOARD (React)

Single page, WebSocket-fed, 1s update cadence:
- Live RT price chart (current location) with charge/discharge shading.
- Fleet target kW vs actual kW line.
- Worker grid: 20 tiles, green/red by heartbeat; dead-letter count.
- Running revenue counter (demo-speed replay).
- Buttons: start/pause replay, speed selector, CHAOS KILL, run backtest.
- Final "pitch" view: big number = fleet USD/year from last backtest run.

## 11. REPO LAYOUT

```
fleet-sim/
  config.yaml            # battery defaults, fleet size, thresholds, hubs
  spec/FLEET_SIM_SPEC.md # this file
  src/
    core/models.py       # dataclasses (section 4)
    core/interfaces.py   # Protocols (FROZEN)
    feeds/replay.py      # PriceFeed impl over Postgres
    ingest/ercot.py      # data pull -> Postgres (gridstatus or direct)
    sim/battery.py       # physics
    sim/worker.py        # worker process entrypoint
    sim/coordinator.py   # tick loop, allocation, failure recovery
    strategy/bands.py    # v1 percentile strategy
    strategy/peak.py     # trivial alt strategy
    ledger/backtest.py
    api/main.py          # FastAPI: ws /live, POST /chaos/kill, POST /backtest
  web/                   # React + Vite dashboard
  tests/
    test_contracts_*.py  # seam tests (write FIRST)
    test_idempotency.py
    test_recovery.py
```

## 12. MILESTONES (build in this order; commit each)

- M0 Walking skeleton: 1 battery, hardcoded 24h price list, tick loop,
  ledger accumulates. End-to-end in-process. ACCEPT: summary() returns
  nonzero USD.
- M1 Data: >= 1 year real ERCOT RT (+ DA if easy) for >= 2 hubs in
  Postgres via ingest/ercot.py. ACCEPT: row counts printed; gap report
  (missing ticks %) printed; human signs off.
- M2 Fleet: 10,000 batteries across 20 worker processes; coordinator
  allocates targets; telemetry aggregates. ACCEPT: 1 sim day runs; actual
  tracks target within 5% with all workers healthy.
- M3 Chaos: kill any worker. ACCEPT: cohort reassigned <= 1 tick; fleet
  within 5% of target <= 3 ticks; idempotency test proves no
  double-applied commands; failures visible in dead_letter.
- M4 Backtest: full-year replay < 10 min. ACCEPT: backtest_runs row with
  fleet USD, per-battery avg, per-location, top spike days.
- M5 Dashboard: all section-10 elements live. ACCEPT: human runs demo
  script end-to-end.
- M6 Demo hardening: 3-minute script (load prices -> chaos kill x2 ->
  pitch number) passes 3 consecutive runs.

## 13. AGENT WORKING RULES

- One module at a time, against this spec. Do not cross seams.
- Write/green the seam contract tests before implementations.
- If a spec detail is ambiguous or an external fact is unverified
  (section 5.1, laptop perf, efficiency default), STOP and ask the human;
  do not assume silently.
- Every claim of "works" needs a runnable command the human can execute.
- Keep a RUNLOG.md: what was built, how to run it, open questions.

## 14. UNVERIFIED ITEMS (carry into RUNLOG.md)

- ERCOT endpoints / gridstatus ERCOT coverage / history depth / rate
  limits — verify hour 0.
- 10,000 agents at 5-min ticks on one laptop — load-test hour 2.
- efficiency=0.90 default — assumption, human-adjustable.
- Revenue numbers valid only from real ingested data; never fabricate
  prices.
