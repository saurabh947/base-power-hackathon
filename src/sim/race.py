"""The live race (docs/API.md `Race`, event kind `race_update`): a shadow copy of the fleet running the
comparison strategy side by side with the live one, inside the coordinator's process.

- Comparison strategy (`comparison_strategy`): `bands` ("Simple rule"), or `forecast` when the live run itself
  uses `bands`. One Strategy instance per city (RUNLOG "Per-location objects").
- Start state: the same batteries as the live fleet (src.sim.fleet.generate_batteries + assign_locations, as
  src.backtest.engine.build_fleets builds them for the backtest) at the run's starting charge.
- Each tick, per city, exactly the backtest engine's steps (src/backtest/engine.py):
    1. Strategy.target_kw(decision_tick(now, published), fleet_soc, H, history) through the caller's `decide`,
       which the coordinator builds over the live run's own PriceWindow: the same prices, the same decision
       lag and the same trailing history as the live fleet.
    2-4. engine.deliver: headroom_kw, allocate, battery.step, grid kWh metered from the SoC change.
    Then the kWh are booked at that tick's RT price as the Ledger books them (usd = kWh x price / 1000, summed
    per city in tick order), so a race over a window earns exactly what src.backtest earns there
    (tests/test_race.py: bit for bit).
- It mirrors the live availability overrides (an offline battery has no headroom and holds its charge) but
  not controller crashes: it has no controllers.

The potential (docs/API.md race.potential) is a second shadow fleet of the same class: the LIVE strategy (the
live run's own Strategy instances, which are pure) with every battery online and no controller crashes. It
books its revenue in the live ledger's order (per tick, city by city in config order: `running_usd`), so with no
outage it earns exactly the live fleet's totals.revenue_usd, to the bit (tests/test_race_fleet.py, and the
20,000-battery bench). shortfall_usd = potential - live: what the outages cost (negative if an outage happened to
save charge for a later, higher price).

Plain-language events (docs/UX.md tone), at most one start and one end per price spike: a spike begins when any
city's 5-minute price reaches the spike threshold ($1,000/MWh) and ends once every city is back below half of
it (the same hysteresis as the `price_spike` event).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

import numpy as np

from src.backtest.engine import Fleet, deliver
from src.sim import geo
from src.sim.allocation import fleet_headroom_kw
from src.sim.battery import fleet_soc

ACTION_EPS_KW = 1e-6  # as the tick record: |feasible| at or below this is "hold"

# How messages name each side: "The AI fleet had 54% charge left; the simple rule had 21%."
SIDE_NOUN = {"forecast": "the AI fleet", "bands": "the simple rule", "peak": "the evening-peak rule"}
SIDE_SUBJECT = {"forecast": "the AI", "bands": "the simple rule", "peak": "the evening-peak rule"}
AI = "forecast"


def comparison_strategy(live_strategy: str) -> str:
    """docs/API.md: the race runs the simple rule, or the AI forecast when the live run itself uses the simple rule."""
    return "forecast" if live_strategy == "bands" else "bands"


def potential_label(live_label: str) -> str:
    """'AI forecast (TimesFM-3)' -> 'AI forecast, all batteries online' (docs/API.md race.potential.label)."""
    return f"{live_label.split(' (')[0]}, all batteries online"


def action(feasible_kw: float) -> str:
    return "sell" if feasible_kw > ACTION_EPS_KW else "charge" if feasible_kw < -ACTION_EPS_KW else "hold"


@dataclass
class RaceCity:
    fleet: Fleet
    soc: np.ndarray
    revenue_usd: float = 0.0
    sold_kwh: float = 0.0
    bought_kwh: float = 0.0
    feasible_kw: float = 0.0  # at the latest tick
    delivered_kwh: float = 0.0  # at the latest tick

    def charge_level(self) -> float:
        """Capacity-weighted SoC of all the city's batteries (as Tick.charge_level)."""
        cap = self.fleet.capacity_kwh
        return float((self.soc * cap).sum() / cap.sum())


@dataclass
class Race:
    """The shadow fleet. `step` once per live tick; `public` is the docs/API.md Race block."""

    strategy: str
    label: str
    cities: dict[str, RaceCity]
    dt_h: float
    soc_min: float
    soc_max: float
    ticks: int = 0
    last_ms: float = 0.0
    spike: dict | None = field(default=None)  # the open spike episode: {"k", "ts", "live_usd", "race_usd"}
    running_usd: float = 0.0  # revenue summed per tick, city by city in config order: the live ledger's order

    @classmethod
    def start(cls, strategy: str, label: str, fleets: Mapping[str, Fleet], soc0: float, *, dt_h: float,
              soc_min: float, soc_max: float) -> "Race":
        cities = {loc: RaceCity(f, np.full(f.size, float(soc0), dtype=np.float64)) for loc, f in fleets.items()}
        return cls(strategy, label, cities, dt_h, soc_min, soc_max)

    def step(self, decide: Callable[[str, float, float], tuple[float, object]], offline: Mapping[str, np.ndarray]) -> None:
        """One tick for every city. decide(location, fleet_soc, H) -> (target kW, the tick being delivered or None);
        offline[location]: the city's availability overrides (True = offline), in battery order."""
        t0 = time.perf_counter()
        for loc, c in self.cities.items():
            f = c.fleet
            avail = ~np.asarray(offline[loc], dtype=bool)
            target, now = decide(loc, fleet_soc(c.soc, f.capacity_kwh, avail), fleet_headroom_kw(f.power_kw, avail))
            target = float(target) if math.isfinite(target) else 0.0
            feasible, kwh, new_soc = deliver(target, c.soc, f.capacity_kwh, f.power_kw, f.efficiency, avail, self.dt_h,
                                             self.soc_min, self.soc_max)
            delivered = float(kwh.sum()) if kwh is not None else 0.0
            c.soc, c.feasible_kw, c.delivered_kwh = new_soc, feasible, delivered
            if now is not None:  # booked as BacktestLedger.record books it
                usd = delivered * float(now.price_usd_mwh) / 1000.0
                c.revenue_usd = c.revenue_usd + usd
                self.running_usd += usd
                if delivered > 0:
                    c.sold_kwh += delivered
                elif delivered < 0:
                    c.bought_kwh -= delivered
        self.ticks += 1
        self.last_ms = (time.perf_counter() - t0) * 1000.0

    def revenue_usd(self) -> float:
        return math.fsum(c.revenue_usd for c in self.cities.values())  # as Ledger.summary()'s total

    def charge_level(self) -> float:
        cap = sum(float(c.fleet.capacity_kwh.sum()) for c in self.cities.values())
        return sum(float((c.soc * c.fleet.capacity_kwh).sum()) for c in self.cities.values()) / cap

    def public(self, live_revenue_usd: float, potential: "Race | None" = None, shows_potential: bool = False) -> dict:
        """docs/API.md Race, with `potential` (null if the potential fleet couldn't run) and `shows_potential`."""
        total = self.revenue_usd()
        return {
            "strategy": self.strategy,
            "label": self.label,
            "revenue_usd": round(total, 6),
            "by_location": {loc: {"revenue_usd": round(c.revenue_usd, 6), "charge_level": round(c.charge_level(), 5),
                                  "action": action(c.feasible_kw)} for loc, c in self.cities.items()},
            "charge_level": round(self.charge_level(), 5),
            "live_minus_race_usd": round(float(live_revenue_usd) - total, 6),
            "potential": None if potential is None else potential.potential_public(live_revenue_usd),
            "shows_potential": bool(shows_potential),
        }

    def potential_public(self, live_revenue_usd: float) -> dict:
        """docs/API.md race.potential: this fleet as the live strategy with every battery online."""
        return {
            "label": self.label,
            "revenue_usd": round(self.running_usd, 6),
            "by_location": {loc: {"revenue_usd": round(c.revenue_usd, 6)} for loc, c in self.cities.items()},
            "shortfall_usd": round(self.running_usd - float(live_revenue_usd), 6),
        }


# --- plain-language race_update messages ------------------------------------------------------------


def _lead_side(live: str, race: str) -> tuple[str, str]:
    """(first, second): the AI is named first when it is in the race, else the live fleet."""
    return (race, live) if race == AI and live != AI else (live, race)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def spike_started_message(prices: Mapping[str, float], total_cities: int, live: str, live_level: float, race: str,
                          race_level: float, fleet_share: Mapping[str, float] | None = None) -> str:
    """'Prices jumped to $1,757 in all four cities. The AI fleet had 54% charge left; the simple rule had 21%.'

    prices: the spiking cities' 5-minute prices. Close prices (within 5%) are shown as one, weighted by each
    city's share of the fleet; otherwise the highest ("as much as").
    """
    locs = list(prices)
    top = max(prices.values())
    if len(locs) == 1:
        head = f"The price in {geo.city_name(locs[0])} jumped to ${top:,.0f}."
    else:
        where = geo.cities_phrase(locs, total=total_cities)
        if (top - min(prices.values())) <= 0.05 * abs(top):
            w = {loc: float((fleet_share or {}).get(loc, 1.0)) for loc in locs}
            price = sum(prices[loc] * w[loc] for loc in locs) / sum(w.values())
            head = f"Prices jumped to ${price:,.0f} in {where}."
        else:
            head = f"Prices jumped to as much as ${top:,.0f} in {where}."
    levels = {live: live_level, race: race_level}
    a, b = _lead_side(live, race)
    return f"{head} {_cap(SIDE_NOUN.get(a, a))} had {levels[a]:.0%} charge left; {SIDE_NOUN.get(b, b)} had {levels[b]:.0%}."


def subject_lead(live: str, race: str, live_minus_race_usd: float) -> float:
    """The lead of the side the messages name first (the AI when it races, else the live fleet)."""
    a, _ = _lead_side(live, race)
    return live_minus_race_usd if a == live else -live_minus_race_usd


def spike_ended_message(live: str, race: str, live_minus_race_usd: float) -> str:
    """'The AI is now $12,400 ahead of the simple rule.' (or behind, or level), over the whole run so far."""
    a, b = _lead_side(live, race)
    lead = subject_lead(live, race, live_minus_race_usd)
    subject, other = SIDE_SUBJECT.get(a, a), SIDE_SUBJECT.get(b, b)
    amount = round(abs(lead))
    if amount == 0:
        return f"{_cap(subject)} and {other} have earned the same so far."
    return f"{_cap(subject)} is now ${amount:,.0f} {'ahead of' if lead > 0 else 'behind'} {other}."
