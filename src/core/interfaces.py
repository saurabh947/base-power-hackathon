"""Section-4 Protocols of spec/FLEET_SIM_SPEC.md, copied VERBATIM. FROZEN seams.

Later stages swap the implementations behind these: replay prices become live
prices, sim batteries become hardware APIs, the rule strategy becomes a solver,
and the pretend ledger becomes real billing. The class docstrings pin the
semantics agreed in RUNLOG.md. Never change a signature. Every implementation
must pass tests/test_contracts_*.py.
"""
from datetime import datetime
from typing import Iterator, Protocol

from src.core.models import BatteryState, Command, PriceTick

__all__ = ["BatteryAdapter", "BatteryState", "Command", "Ledger", "PriceFeed",
           "PriceTick", "Strategy"]


class PriceFeed(Protocol):
    """Replays (later: streams) real-time prices.

    ticks(start, end, location) yields PriceTicks for `location` only:
    - ts is tz-aware UTC, strictly increasing, and start <= ts < end.
    - start and end must be tz-aware (any zone; compared as instants).
      Naive datetimes raise ValueError.
    - An empty range or an unknown location yields nothing.
    - 15-min native prices are forward-filled to 5-min ticks within their own
      interval only; a missing interval stays missing.
    - Replays are deterministic: the same call yields the same ticks.
    """
    def ticks(self, start: datetime, end: datetime,
              location: str) -> Iterator[PriceTick]: ...


class BatteryAdapter(Protocol):
    """Applies commands to, and reads state from, batteries.

    apply(cmd):
    - Idempotent per command_id and per (battery, tick). Each battery keeps
      last_command_id and last_tick. A command whose id equals last_command_id,
      or whose tick_ts <= last_tick, is a silent no-op, so duplicates, replays
      and stale or zombie commands never move energy.
    - Otherwise the grid-side setpoint is clipped to what is feasible for one
      tick (|P| <= power_kw, SoC stays within [0.20, 1.00]) and applied, and
      the fence advances to this command.
    - An unavailable battery moves no energy, but its fence still advances.
    - Raises KeyError for an unknown battery_id. Raises ValueError, leaving
      state unchanged, for a naive tick_ts or a non-finite setpoint.
    read(battery_id): returns a snapshot (mutating it changes nothing).
    Raises KeyError for an unknown battery_id.
    """
    def apply(self, cmd: Command) -> None: ...      # idempotent per command_id
    def read(self, battery_id: str) -> BatteryState: ...


class Strategy(Protocol):
    """Turns what the fleet knows now into one fleet-level grid-side target.

    target_kw(tick, fleet_soc, fleet_headroom_kw, price_history) -> kW:
    - tick: the latest price tick the strategy may see. The caller applies
      the decision lag (RUNLOG decision 5). With lag 3 this is the last
      published 15-min price, 3 ticks behind the tick being delivered.
    - fleet_soc: capacity-weighted mean SoC of available batteries, in [0, 1].
    - fleet_headroom_kw: symmetric H = sum of power_kw of available batteries.
    - price_history: USD/MWh of the ticks strictly BEFORE `tick` at the same
      location, oldest first. The strategy uses only its own trailing window.
    - Returns a finite value in [-H, +H] (+ = discharge/sell, - = charge/buy),
      and 0 when H == 0. The caller clips it to the feasible directional
      headroom and splits it across batteries in proportion to each one's
      feasible headroom in that direction.
    - Pure: no side effects, no state between calls, never mutates its inputs.
    """
    def target_kw(self, tick: PriceTick,
                  fleet_soc: float,
                  fleet_headroom_kw: float,
                  price_history: list[float]) -> float: ...


class Ledger(Protocol):
    """Values delivered energy (pretend billing now, real billing later).

    record(tick, delivered_kwh): grid-side energy moved during `tick` at
    tick.location, valued at that tick's RT price:
    usd = delivered_kwh * price_usd_mwh / 1000. Buying at a negative price
    therefore earns money. Records are additive (two records for one tick
    sum). Non-finite values raise ValueError.
    summary() returns a dict with exactly these keys:
    - total_usd: float
    - per_location_usd: {location: usd}, covering every configured location
    - per_battery_avg_usd: total_usd / fleet size, over the recorded period
    - sold_kwh, bought_kwh: floats >= 0
    - top_spike_days: up to 10 [{"date": "YYYY-MM-DD", "usd": float}],
      highest revenue first; days are America/Chicago local dates.
    """
    def record(self, tick: PriceTick, delivered_kwh: float) -> None:
        """delivered_kwh: + = sold to grid, - = bought."""
    def summary(self) -> dict: ...   # totals, per-location, per-battery avg
