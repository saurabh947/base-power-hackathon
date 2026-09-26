"""The live race inside the real coordinator and worker processes (small fleet, real snapshot prices, Redis):
the Race block in the snapshot, race_update events at a real price spike, the race earning exactly what
src.backtest earns for the comparison strategy (bit for bit) while the live fleet runs next to it, availability
mirrored, crashes not, null while idle, and the AI forecast racing a live simple rule (SYNTHETIC forecast books:
test doubles, labelled as such; a local checkout has no TimesFM forecasts)."""
import json
import os
import re
import signal
import uuid
from datetime import datetime, timedelta

import numpy as np
import pytest

from src.backtest.engine import build_fleets, load_ticks
from src.backtest.runner import replay
from src.config import REPO_ROOT, load_config, load_raw_config
from src.sim import coordinator as coord
from src.sim.coordinator import Coordinator, price_feed, snapshot_years
from src.sim.redis_layout import reachable, redis_url
from src.strategy import make_strategy

pytestmark = pytest.mark.redis
CFG = load_config()
LOCS = list(CFG.fleet.locations)
SUMMER_SPIKE = "2025-07-11T23:00:00Z"  # 6 pm CDT: all four cities jump to ~$1,757/MWh at 9 pm (tick 36), 9:15 back
SIZE, WORKERS = 400, 4


@pytest.fixture
def co():
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    orch = coord.Orchestration.from_config(load_raw_config(), max_speed_deadline_ms=1000.0)  # as run_fleet_replay
    c = Coordinator(url=url, prefix=f"test:race:{uuid.uuid4().hex[:8]}:", fleet_size=SIZE, workers=WORKERS,
                    orchestration=orch)
    c.boot()
    yield c
    c.shutdown()
    c.cleanup_keys()


def backtest(name, start_utc, ticks, soc0, strategies=None):
    """src.backtest over the same window from the same start state (as tests/test_fleet_backtest_parity.py)."""
    start = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    end = start + timedelta(seconds=ticks * CFG.sim.tick_seconds)
    series = load_ticks(price_feed(snapshot_years(start), LOCS), LOCS, start, end,
                        start - timedelta(days=CFG.strategy.bands.window_days))
    fleets = build_fleets(SIZE, CFG.fleet.locations, CFG.battery, CFG.seed, soc_init=soc0)
    return replay(name, CFG, series, fleets, lag=CFG.sim.decision_lag_ticks, strategies=strategies)


def race_contract() -> dict:
    text = (REPO_ROOT / "docs" / "API.md").read_text()
    return json.loads(re.search(r"\*\*Race\*\*.*?```json\s*(\{.*?\})\s*```", text, re.S).group(1))


def race_events(c):
    return [e for e in c.events if e["kind"] == "race_update"]


def test_the_race_runs_next_to_the_live_fleet(co):
    assert co.snapshot()["race"] is None  # idle: no race
    co.start({"start_utc": SUMMER_SPIKE, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=42, internal=True)
    assert co.run.race.strategy == "bands" and co.run.race.label == "Simple rule"
    assert co.serve_until(lambda: co.run.status == "finished", timeout_s=90)
    race, snap = co.run.race, co.snapshot()
    assert race.ticks == 42
    # docs/API.md Race, in the snapshot the API serves (finished runs keep it)
    block, example = snap["race"], race_contract()
    assert set(block) == set(example) and list(block["by_location"]) == LOCS
    assert all(set(v) == set(example["by_location"]["LZ_HOUSTON"]) for v in block["by_location"].values())
    assert block["live_minus_race_usd"] == pytest.approx(snap["totals"]["revenue_usd"] - block["revenue_usd"], abs=1e-5)
    # the race earned exactly what the backtest earns for the simple rule over these 42 ticks, city by city
    bands = backtest("bands", SUMMER_SPIKE, 42, 0.9)
    for loc in LOCS:
        assert race.cities[loc].revenue_usd == bands.summary["per_location_usd"][loc], loc
        assert np.array_equal(race.cities[loc].soc, bands.runs[loc].soc_final), loc
    # ... while the live fleet (the evening peak, through the workers) earned what the backtest earns for peak
    peak = backtest("peak", SUMMER_SPIKE, 42, 0.9)
    assert co.run.ledger.summary() == peak.summary
    assert block["live_minus_race_usd"] == pytest.approx(peak.summary["total_usd"] - bands.summary["total_usd"],
                                                         abs=1e-5)
    # one race_update when the 9 pm spike begins, one when it is over (9:15 pm), in plain words
    start, end = race_events(co)
    assert (start["sim_utc"], end["sim_utc"]) == ("2025-07-12T02:00:00Z", "2025-07-12T02:15:00Z")
    live_level, race_level = start["detail"]["live"]["charge_level"], start["detail"]["race"]["charge_level"]
    assert start["message"] == (f"Prices jumped to $1,757 in all four cities. The evening-peak rule had "
                                f"{live_level:.0%} charge left; the simple rule had {race_level:.0%}.")
    assert set(start["detail"]["prices"]) == set(LOCS) and start["severity"] == "info"
    lead = end["detail"]["live_minus_race_usd"]
    word = "ahead of" if lead > 0 else "behind"
    assert end["message"] == f"The evening-peak rule is now ${abs(lead):,.0f} {word} the simple rule."
    assert end["severity"] == ("good" if lead > 0 else "info")  # good only when the live strategy leads
    assert end["detail"]["race"]["during_spike_usd"] > 0  # the simple rule was still selling at $1,757
    # the charge levels are the fleets' just before the spike's first 5-minute step (tick 36)
    assert race_level == pytest.approx(float(np.average(
        np.concatenate([backtest("bands", SUMMER_SPIKE, 36, 0.9).runs[loc].soc_final for loc in LOCS]),
        weights=np.concatenate([build_fleets(SIZE, CFG.fleet.locations, CFG.battery, CFG.seed)[loc].capacity_kwh
                                for loc in LOCS]))), abs=1e-5)
    assert all(e["kind"] != "race_update" or e["message"].endswith(".") for e in co.events)


def test_the_race_mirrors_availability_but_not_crashes(co):
    co.start({"start_utc": SUMMER_SPIKE, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=40, internal=True)
    for _ in range(24):  # to 8 pm, while the simple rule sells
        co.step()
    reply = co.handle_control({"cmd": "availability", "args": {"scope": "location", "location": "LZ_HOUSTON",
                                                                "offline_fraction": 0.3}})
    assert reply["ok"]
    held = co.availability.offline[co.loc_slice["LZ_HOUSTON"]].copy()
    assert held.sum() == round(0.3 * held.size)
    before = co.run.race.cities["LZ_HOUSTON"].soc.copy()
    victim = co.workers[1]
    os.kill(victim.pid, signal.SIGKILL)  # a controller crash: the live fleet feels it, the race does not
    for _ in range(12):
        co.step()
    assert victim.status == "dead"
    after = co.run.race.cities["LZ_HOUSTON"].soc
    assert np.array_equal(after[held], before[held]) and (after[~held] < before[~held]).all()
    assert co.snapshot()["race"]["by_location"]["LZ_HOUSTON"]["action"] in ("sell", "hold")


def test_no_forecasts_means_no_race_against_the_ai(co):
    """The live simple rule races the AI forecast; with no forecasts for any city there is nothing to race."""
    coord._STRATEGY_CACHE.clear()
    co.start({"start_utc": SUMMER_SPIKE, "speed_tps": "max", "strategy": "bands"}, max_ticks=2, internal=True)
    assert co.run.strategy_name == "bands" and co.run.race is None
    assert co.serve_until(lambda: co.run.status == "finished", timeout_s=30)
    assert co.snapshot()["race"] is None and not race_events(co)


def _synthetic_books(start_s: int):
    """SYNTHETIC forecast books (test doubles, not TimesFM): hourly issues, a fixed daily shape."""
    from src.forecast.provider import ForecastBook

    issued = (start_s - 8 * 86400 + np.arange(9 * 24 + 2, dtype=np.int64) * 3600) * 10**9  # to a day past the start
    hour = ((issued // 10**9 // 3600) % 24).astype(np.float32)
    shape = 40.0 + 30.0 * np.sin((hour[:, None] + np.arange(96, dtype=np.float32)[None, :] / 4) / 24 * 2 * np.pi)
    return {loc: ForecastBook(loc, "rt_price", "synthetic", 15, issued, issued.copy(), shape - 5, shape, shape + 5)
            for loc in LOCS}


def test_the_ai_races_a_live_simple_rule(co, monkeypatch):
    import src.strategy
    from src.strategy.forecast import ForecastStrategy

    start_s = int(datetime.fromisoformat(SUMMER_SPIKE.replace("Z", "+00:00")).timestamp())
    books = _synthetic_books(start_s)
    real = src.strategy.make_strategy

    def make(cfg, tick_seconds, name=None, **context):
        if name == "forecast":
            wanted = context.get("locations") or LOCS
            return ForecastStrategy({loc: books[loc] for loc in wanted}, duration_h=2.0)
        return real(cfg, tick_seconds, name, **context)

    monkeypatch.setattr(src.strategy, "make_strategy", make)
    monkeypatch.setattr(coord, "_STRATEGY_CACHE", {})
    co.start({"start_utc": SUMMER_SPIKE, "speed_tps": "max", "strategy": "bands", "start_charge_level": 0.9},
             max_ticks=40, internal=True)
    race = co.run.race
    assert race.strategy == "forecast" and race.label == "AI forecast (TimesFM-3)"
    assert co.serve_until(lambda: co.run.status == "finished", timeout_s=90)
    ai = backtest("forecast", SUMMER_SPIKE, 40, 0.9, strategies={loc: make(CFG.strategy, 300, "forecast",
                                                                           locations=[loc]) for loc in LOCS})
    for loc in LOCS:
        assert race.cities[loc].revenue_usd == ai.summary["per_location_usd"][loc], loc
    start, end = race_events(co)
    assert start["message"].startswith("Prices jumped to $1,757 in all four cities. The AI fleet had ")
    assert re.fullmatch(r"The AI is now \$[\d,]+ (ahead of|behind) the simple rule\.|The AI and the simple rule have "
                        r"earned the same so far\.", end["message"])
    lead = end["detail"]["live_minus_race_usd"]  # the live simple rule vs the AI race
    assert end["severity"] == ("good" if round(lead) > 0 else "info")
    assert ("ahead of" in end["message"]) == (lead < 0)  # the AI (the race) is named first
    assert co.snapshot()["race"]["label"] == "AI forecast (TimesFM-3)"


def test_labels_come_from_config(monkeypatch):
    """The race's label and the run_started sentence use config backtest.labels, like the Results and /settings."""
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    raw = load_raw_config()
    raw = {**raw, "backtest": {**raw["backtest"], "labels": {**raw["backtest"]["labels"], "bands": "Simple rule (v1)",
                                                               "peak": "Evening peak (6-9 pm)"}}}
    c = Coordinator(url=url, prefix=f"test:race:{uuid.uuid4().hex[:8]}:", fleet_size=40, workers=1, raw=raw)
    try:
        c.boot()
        c.start({"start_utc": SUMMER_SPIKE, "speed_tps": "max", "strategy": "peak"}, max_ticks=1, internal=True)
        assert c.run.race.label == "Simple rule (v1)" and c.snapshot()["race"]["label"] == "Simple rule (v1)"
        started = next(e for e in c.events if e["kind"] == "run_started")
        assert "using the evening peak (6-9 pm)." in started["message"]
    finally:
        c.shutdown()
        c.cleanup_keys()
