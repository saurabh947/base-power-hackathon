"""The Results object of docs/API.md (GET /results/pitch), built from a pitch set of records.

Keys are exactly the contract's. Everything else a reader needs (the lag-0 comparison, the
one-cycle-a-day ceiling, a strategy that was skipped and why) is a plain-language line in
`assumptions`, written for the Results screen's "How we got this number" (docs/UX.md).
"""
from __future__ import annotations

from typing import Sequence

from src.sim import geo

FORECAST_LICENCE = ("The AI forecast uses Google's TimesFM-3 research weights, licensed for non-commercial use; "
                    "a production version would use TimesFM 2.5 (Apache-2.0).")


def build_results(records: Sequence[dict], *, year: int, settings: dict | None = None) -> dict:
    from src.backtest.runner import HINDSIGHT, backtest_settings

    settings = settings or backtest_settings()
    latest: dict[str, dict] = {}
    for r in sorted(records, key=lambda r: r.get("finished_at") or ""):
        if r.get("year") in (None, year):
            latest[r["strategy"]] = r
    ceiling = latest.pop(HINDSIGHT, None)
    ran = {k: r for k, r in latest.items() if not r["metrics"].get("skipped")}
    skipped = {k: r for k, r in latest.items() if r["metrics"].get("skipped")}
    order = [s for s in settings["strategies"] if s in latest] + sorted(set(latest) - set(settings["strategies"]))
    headline = next((s for s in settings["headline"] if s in ran), next((s for s in order if s in ran), None))
    labels = settings["labels"]

    source = ran.get(headline) or next(iter(latest.values()), None) or ceiling
    fleet = (source or {}).get("params", {}).get("fleet", {})
    loc_order = list(fleet.get("locations") or fleet.get("by_location") or [])

    def by_loc(values: dict) -> dict:
        keys = [k for k in loc_order if k in values] + sorted(set(values) - set(loc_order))
        return {k: _money(values[k]) for k in keys}

    strategies = []
    for name in order:
        if name not in ran:
            continue
        r, m = ran[name], ran[name]["metrics"]
        strategies.append({
            "strategy": name,
            "label": labels.get(name) or r.get("label") or name,
            "run_id": r["run_id"],
            "is_headline": name == headline,
            "per_battery_usd_year": _money(m["per_battery_usd_year"]),
            "fleet_usd_year": _money(m["fleet_usd_year"]),
            "usd_per_kwh_year": _money(m["usd_per_kwh_year"]),
            "usd_per_kw_year": _money(m["usd_per_kw_year"]),
            "cycles_per_year": round(float(m["cycles_per_year"]), 1),
            "per_location_usd": by_loc(r["per_location"] or {}),
            "top_days": [{"date_local": d["date_local"], "usd": _money(d["usd"])} for d in (r["top_days"] or [])],
        })

    everything = [*latest.values(), *([ceiling] if ceiling else [])]
    finished = [r["finished_at"] for r in everything if r.get("finished_at")]
    return {
        "year": year,
        "generated_utc": max(finished) if finished else None,
        "fleet_size": fleet.get("size"),
        "fleet_power_mw": round(fleet["power_kw_total"] / 1000.0, 1) if "power_kw_total" in fleet else None,
        "fleet_energy_mwh": round(fleet["capacity_kwh_total"] / 1000.0, 1) if "capacity_kwh_total" in fleet else None,
        "assumptions": assumptions(year, source, ran, skipped, headline, ceiling, labels),
        "strategies": strategies,
        "ceiling": None if ceiling is None else {
            "label": labels.get(HINDSIGHT) or ceiling.get("label"),
            "per_battery_usd_year": _money(ceiling["metrics"]["per_battery_usd_year"]),
            "fleet_usd_year": _money(ceiling["metrics"]["fleet_usd_year"]),
        },
    }


def assumptions(year, source, ran, skipped, headline, ceiling, labels) -> list[str]:
    params = (source or {}).get("params", {})
    fleet = params.get("fleet", {})
    ranges = fleet.get("battery_ranges", {})
    sim = params.get("config", {}).get("sim", {})
    lines = [
        f"Gross wholesale energy value at real-time prices from ERCOT, the Texas grid operator "
        f"(15-minute settlement), for every 5-minute step of {year}.",
        "Excludes retail and delivery charges, payments for grid-support services, and battery wear.",
        "The fleet is assumed too small to move market prices.",
    ]
    if fleet and ranges:
        (p_lo, p_hi), (c_lo, c_hi) = ranges["power_kw"], ranges["capacity_kwh"]
        lines.append(
            f"{fleet['size']:,} simulated home batteries of {p_lo:g}–{p_hi:g} kW and {c_lo:g}–{c_hi:g} kWh each "
            f"({fleet['power_kw_total'] / 1000:,.0f} MW and {fleet['capacity_kwh_total'] / 1000:,.0f} MWh in all), "
            f"{_split(fleet.get('by_location', {}), fleet.get('locations'))}.")
        soc_min, eff = ranges["soc_bounds"][0], ranges["efficiency"]
        lines.append(f"Batteries never go below {soc_min:.0%} charge (a safety reserve), and a round trip "
                     f"(charge, then discharge) returns {eff:.0%} of the energy.")
        lines.append(f"One cycle means using a battery's full usable energy once, from 100% down to the "
                     f"{soc_min:.0%} reserve.")
    lag = params.get("lag_ticks")
    if lag and sim.get("tick_seconds"):
        late = lag * sim["tick_seconds"] // 60
        line = f"Decisions use each price only once it is published, {late} minutes late."
        lag0 = ran.get(headline, {}).get("metrics", {}).get("lag0") if headline else None
        if lag0:
            line += (f" If prices were known instantly, {labels.get(headline, headline)} would earn "
                     f"${lag0['per_battery_usd_year']:,.2f} per battery instead.")
        lines.append(line)
    if ceiling:
        m = ceiling["metrics"]
        lines.append(
            f"{labels.get('hindsight', 'Best possible')} assumes perfect knowledge of each day's prices, with each "
            f"day planned on its own. No real system can reach it. Held to {m['cycles_per_day_cap']:g} full cycle a "
            f"day, it would be ${m['capped']['per_battery_usd_year']:,.2f} per battery.")
    coverage = min((r["metrics"].get("coverage", {}).get("fraction", 1.0) for r in ran.values()), default=1.0)
    if coverage < 0.9999:
        lines.append(f"Prices were missing for {1 - coverage:.2%} of the year's 5-minute steps; "
                     f"the batteries sat idle then.")
    if "forecast" in ran:
        lines.append(FORECAST_LICENCE)
    for name, r in skipped.items():
        lines.append(f"The {labels.get(name, name)} method isn't in these results: "
                     f"{r['metrics'].get('reason_plain') or 'it could not run.'}")
    return lines


def _split(by_location: dict, order: list | None = None) -> str:
    """'in all four cities: Houston (7,000), Dallas–Fort Worth (8,000), San Antonio (2,600) and Austin (2,400)'.
    City names come from src.sim.geo, the one place that maps a load zone to a city."""
    locs = [k for k in (order or []) if k in by_location] + [k for k in by_location if k not in (order or [])]
    names = [geo.city_name(loc) for loc in locs]
    counts = [by_location[loc] for loc in locs]
    if len(names) == 2 and counts[0] == counts[1]:
        return f"split evenly between {names[0]} and {names[1]}"
    parts = geo.join_names([f"{n} ({c:,})" for n, c in zip(names, counts)])
    return f"in {geo.cities_phrase(locs, total=len(locs))}: {parts}" if len(locs) > 1 else f"in {parts}"


def _money(x) -> float | None:
    return None if x is None else round(float(x), 2)


# --- GET /results/ai-gains -------------------------------------------------------------------------

AI, BASELINE = "forecast", "bands"
GAIN_ORDER = ("forecast", "bands", "peak")  # the StrategyGains keys, in the contract's order
INSIGHT_FIELDS = (  # StrategyGains field <- metrics.insights, decimals
    ("surplus_mwh", 1), ("spike_mwh", 1), ("spike_events", None), ("spike_charge_level", 3),
    ("sold_mwh", 1), ("bought_mwh", 1), ("avg_sell_usd_mwh", 2), ("avg_buy_usd_mwh", 2),
)


def fleet_signature(record: dict | None) -> tuple | None:
    """(size, ((location, batteries), ...)) of a slim record's fleet: runs are only compared on one fleet."""
    fleet = (record or {}).get("fleet") or {}
    by_location = fleet.get("by_location") or {}
    if fleet.get("size") is None or not by_location:
        return None
    return int(fleet["size"]), tuple(sorted((str(k), int(v)) for k, v in by_location.items()))


def config_fleet_signature(cfg=None) -> tuple:
    from src.config import load_config
    from src.sim.fleet import location_counts

    cfg = cfg or load_config()
    return int(cfg.fleet.size), tuple(sorted(location_counts(cfg.fleet.size, cfg.fleet.locations).items()))


def _newest(records):
    return max(records, key=lambda r: (r.get("finished_at") or "", r.get("started_at") or ""), default=None)


def build_ai_gains(records: Sequence[dict], *, pitch_year: int, settings: dict | None = None,
                   default_signature: tuple | None = None) -> dict:
    """docs/API.md AiGains from slim records (src.backtest.store.load_gains_records / load_gains_records_json).

    - The pitch year: its is_pitch set (the newest flagged run per strategy), listed when it has a simple-rule
      run. `uplift` (AI minus simple rule; pct = uplift / simple rule) needs the AI run too, so it and the AI's
      entry are absent in a build without forecasts; in the cloud both exist.
    - Every other year: the latest complete set, i.e. the newest successful bands, forecast and peak runs on the
      pitch set's fleet (same size and cities; config.yaml's fleet when there is no pitch set). A year without
      all three is left out.
    - best_possible_per_battery_usd_year: that year's newest hindsight run on the same fleet; it and every
      share_of_best (per-battery earnings / it) are absent when the year has none.
    - The insight fields come from metrics.insights. A run stored before insights existed has none of them:
      they are left out of its StrategyGains (the money fields are always there). `spike_events` is the year's
      count (spikes are defined by prices), the same on every strategy, taken from any run of the year with it.
    - Each year also carries `fleet_size` (the batteries its runs simulated).
    Labels come from config backtest.labels. Years: the pitch year first, then the others, newest first.
    """
    from src.backtest.runner import HINDSIGHT, backtest_settings

    settings = settings or backtest_settings()
    labels = settings["labels"]
    usable = [r for r in records if r.get("year") is not None and r.get("fleet_total_usd") is not None
              and r.get("per_battery_avg_usd") is not None]
    pitch = {}
    for name in (*GAIN_ORDER, HINDSIGHT):
        newest = _newest([r for r in usable if r["is_pitch"] and r["year"] == pitch_year and r["strategy"] == name])
        if newest is not None:
            pitch[name] = newest
    signature = fleet_signature(pitch.get(AI) or pitch.get(BASELINE)) or default_signature
    years = []
    if BASELINE in pitch:
        years.append(_gains_year(pitch_year, pitch, is_pitch=True, labels=labels))
    for year in sorted({r["year"] for r in usable} - {pitch_year}, reverse=True):
        mine = [r for r in usable if r["year"] == year and fleet_signature(r) == signature]
        latest = {name: _newest([r for r in mine if r["strategy"] == name]) for name in (*GAIN_ORDER, HINDSIGHT)}
        if all(latest[name] is not None for name in GAIN_ORDER):
            years.append(_gains_year(year, {k: v for k, v in latest.items() if v is not None}, is_pitch=False,
                                     labels=labels))
    return {"ai": AI, "baseline": BASELINE, "years": years}


def _gains_year(year: int, runs: dict, *, is_pitch: bool, labels: dict) -> dict:
    from src.backtest.runner import HINDSIGHT

    best = runs.get(HINDSIGHT)
    best_pb = float(best["per_battery_avg_usd"]) if best else None
    main = runs.get(BASELINE) or runs.get(AI) or next(iter(runs.values()))
    out: dict = {"year": int(year), "is_pitch": bool(is_pitch), "fleet_size": (main.get("fleet") or {}).get("size")}
    if best_pb is not None:
        out["best_possible_per_battery_usd_year"] = _money(best_pb)
    # spikes are defined by prices, so their count is the year's, the same for every strategy (taken from any run
    # that has insights, so a run stored before insights existed shows it too)
    events = next((r["insights"]["spike_events"] for r in runs.values()
                   if isinstance(r.get("insights"), dict) and r["insights"].get("spike_events") is not None), None)
    out["strategies"] = {name: _strategy_gains(name, runs[name], best_pb, labels, spike_events=events)
                         for name in GAIN_ORDER if name in runs}
    ai, base = runs.get(AI), runs.get(BASELINE)
    if ai is not None and base is not None:
        diff = float(ai["per_battery_avg_usd"]) - float(base["per_battery_avg_usd"])
        base_pb = float(base["per_battery_avg_usd"])
        out["uplift"] = {"per_battery_usd_year": _money(diff),
                         "pct": round(diff / base_pb, 3) if base_pb > 0 else None,
                         "fleet_usd_year": _money(float(ai["fleet_total_usd"]) - float(base["fleet_total_usd"]))}
    return out


def _strategy_gains(name: str, run: dict, best_pb: float | None, labels: dict, *, spike_events: int | None = None) -> dict:
    per_battery = float(run["per_battery_avg_usd"])
    out = {"label": labels.get(name) or run.get("label") or name, "per_battery_usd_year": _money(per_battery),
           "fleet_usd_year": _money(run["fleet_total_usd"])}
    if best_pb:
        out["share_of_best"] = round(per_battery / best_pb, 3)
    insights = run.get("insights") if isinstance(run.get("insights"), dict) else {}
    for key, digits in INSIGHT_FIELDS:
        if key == "spike_events" and spike_events is not None:
            out[key] = int(spike_events)
        elif key in insights:
            value = insights[key]
            out[key] = value if value is None or digits is None else round(float(value), digits)
    return out
