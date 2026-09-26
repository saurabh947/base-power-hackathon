"""Section-4 dataclasses of spec/FLEET_SIM_SPEC.md, copied VERBATIM. FROZEN.

Only docstrings may be added here, to pin semantics agreed in RUNLOG.md.
Never change a field, a type or a default.
"""
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PriceTick:
    """One real-time price for one location and one 5-minute tick.

    - ts: tz-aware UTC start of the tick. ERCOT RT settlement point prices are
      native 15-minute; each interval is forward-filled to its own three ticks
      (ts, ts+5min, ts+10min) and never across a gap.
    - price_usd_mwh: USD/MWh, finite, may be negative. Real ERCOT prices, never invented,
      with one labelled exception: after a year's real_until (2026), replay ticks carry the
      AI's p50 prediction instead, and the API flags those ticks `predicted`.
    - demand_mw / fuel_mix: optional context; None when not ingested.
    """
    ts: datetime            # UTC, 5-minute grid
    location: str           # e.g. "HB_NORTH"
    price_usd_mwh: float
    demand_mw: float | None
    fuel_mix: dict | None   # {"gas": .., "wind": .., ...} fractions


@dataclass
class BatteryState:
    """Snapshot of one battery, as returned by BatteryAdapter.read().

    Power and energy are GRID-SIDE, at the AC meter (RUNLOG decision 1):
    - power_kw bounds the grid-side rate: |P| <= power_kw either way.
    - efficiency is round-trip eta, split sqrt(eta) each way. Discharging
      P kW for dt hours removes P*dt/sqrt(eta) kWh from storage; charging at
      |P| kW adds |P|*dt*sqrt(eta). A round trip returns eta of the grid energy.
    - soc = stored kWh / capacity_kwh, kept within [0.20, 1.00]
      (0.20 = safety reserve floor).
    - available=False: the battery moves no energy.
    """
    battery_id: str
    capacity_kwh: float
    power_kw: float         # max charge/discharge rate
    soc: float              # 0.0-1.0 state of charge
    efficiency: float       # round-trip, e.g. 0.90
    available: bool


@dataclass(frozen=True)
class Command:
    """One setpoint for one battery for one tick.

    - command_id = uuid5(NAMESPACE, f"{run_id}|{battery_id}|{tick_ts UTC isoformat}")
      (src.core.ids.make_command_id): deterministic, so a replayed command
      carries the same id and dedupes.
    - setpoint_kw is GRID-SIDE kW held for the whole tick:
      + = discharge to grid (sell), - = charge (buy).
    - tick_ts: tz-aware UTC start of the 5-minute tick it applies to.
    """
    command_id: str         # uuid; workers MUST dedupe on this
    battery_id: str
    setpoint_kw: float      # + = discharge to grid, - = charge
    tick_ts: datetime
