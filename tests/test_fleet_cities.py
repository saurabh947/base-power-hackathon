"""The four-city fleet through the real coordinator and workers (small fleet): plain-language events name the
cities (docs/UX.md tone), and the AI forecast falls back to the simple rule per city. Real prices from
data/snapshot; the forecast books here are synthetic test doubles, labelled as such."""
import uuid

import numpy as np
import pytest

from src.config import load_config
from src.sim import coordinator as coord
from src.sim.coordinator import Coordinator
from src.sim.redis_layout import reachable, redis_url

pytestmark = pytest.mark.redis
LOCS = list(load_config().fleet.locations)
SPIKE_7PM = "2025-05-17T00:00:00Z"  # 7:00 pm CDT on the spike evening: the four cities cross $1,000/MWh


@pytest.fixture
def co():
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    c = Coordinator(url=url, prefix=f"test:cities:{uuid.uuid4().hex[:8]}:", fleet_size=400, workers=4)
    c.boot()
    yield c
    c.shutdown()
    c.cleanup_keys()


def _messages(c, kind):
    return [e["message"] for e in c.events if e["kind"] == kind]


def test_events_talk_about_the_four_cities(co):
    co.start({"start_utc": SPIKE_7PM, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=12, internal=True)
    assert co.serve_until(lambda: co.run.status == "finished", timeout_s=60)
    started = _messages(co, "run_started")[0]
    assert started == ("Replaying real prices from May 16, 2025, 7:00 pm (Central time) at as fast as the computer "
                       "can go, using the evening peak.")
    # at 7:15 pm three cities cross $1,000/MWh together (one event), at 7:30 pm Houston follows (real prices)
    assert _messages(co, "price_spike") == [
        "Prices jumped in Dallas–Fort Worth, San Antonio and Austin, up to $1,295/MWh ($1.29 per kWh) in San Antonio.",
        "The price in Houston jumped to $2,137/MWh ($2.14 per kWh)."]
    spike = next(e for e in co.events if e["kind"] == "price_spike")
    assert spike["detail"]["location"] == "LZ_CPS"
    assert set(spike["detail"]["prices"]) == {"LZ_NORTH", "LZ_CPS", "LZ_AEN"}
    assert _messages(co, "action_changed") == ["Batteries in all four cities are now selling while prices are high."]
    finished = _messages(co, "run_finished")[0]
    assert "7:55 pm, Central time)" in finished and "Houston time" not in " ".join(e["message"] for e in co.events)
    # the controllers each serve one city, or two at a boundary; totals add up per city
    snap = co.snapshot()
    assert [w["by_location"] for w in snap["workers"]] == [
        co.layout.counts_by_location(np.flatnonzero(co.owner == w)) for w in range(4)]
    for key in ("revenue_usd", "sold_kwh", "bought_kwh"):
        assert sum(v[key] for v in snap["totals"]["by_location"].values()) == pytest.approx(snap["totals"][key],
                                                                                            abs=1e-4)
    assert all(v["sold_kwh"] > 0 for v in snap["totals"]["by_location"].values())


def test_offline_controller_message_names_its_cities(co):
    reply = co.handle_control({"cmd": "availability", "args": {"scope": "worker", "worker_id": 3, "available": False}})
    assert reply["ok"]
    cities = co.layout.counts_by_location(np.flatnonzero(co.owner == 3))
    msg = _messages(co, "availability_changed")[-1]
    from src.sim import geo

    assert msg.startswith(f"Controller 4's 100 batteries in {geo.join_names([geo.city_name(c) for c in cities])} "
                          "are offline;")


def _synthetic_books(start_s: int, locations) -> dict:
    """Synthetic forecast books (test doubles, not real forecasts): hourly issues, 24 h of 15-min points."""
    from src.forecast.provider import ForecastBook

    issued = (start_s - 1800 + np.arange(30, dtype=np.int64) * 3600) * 10**9
    shape = np.tile(np.linspace(20.0, 80.0, 96, dtype=np.float32), (30, 1))
    return {loc: ForecastBook(loc, "rt_price", "synthetic", 15, issued, issued.copy(), shape - 5, shape, shape + 5)
            for loc in locations}


def test_the_ai_forecast_falls_back_per_city(co, monkeypatch):
    """Forecasts for San Antonio and Austin are still being computed: those two cities run the simple rule and
    the run_started event says so; Houston and Dallas-Fort Worth keep the AI forecast."""
    import src.strategy
    from src.strategy.bands import BandsStrategy
    from src.strategy.forecast import ForecastStrategy

    start = 1747429200  # 2025-05-16T21:00:00Z
    books = _synthetic_books(start, ["LZ_HOUSTON", "LZ_NORTH"])
    real = src.strategy.make_strategy

    def make(cfg, tick_seconds, name=None, **context):
        if name == "forecast":
            wanted = context.get("locations") or list(books)
            return ForecastStrategy({loc: books[loc] for loc in wanted if loc in books}, duration_h=2.0)
        return real(cfg, tick_seconds, name, **context)

    monkeypatch.setattr(src.strategy, "make_strategy", make)
    monkeypatch.setattr(coord, "_STRATEGY_CACHE", {})
    co.start({"start_utc": "2025-05-16T21:00:00Z", "speed_tps": "max", "strategy": "forecast"}, max_ticks=3,
             internal=True)
    run = co.run
    assert run.strategy_name == "forecast" and list(run.strategy_fallback) == ["LZ_CPS", "LZ_AEN"]
    kinds = {loc: type(s) for loc, s in run.decide.strategies.items()}
    assert kinds == {"LZ_HOUSTON": ForecastStrategy, "LZ_NORTH": ForecastStrategy, "LZ_CPS": BandsStrategy,
                     "LZ_AEN": BandsStrategy}
    event = next(e for e in co.events if e["kind"] == "run_started")
    assert event["severity"] == "warn" and event["message"] == (
        "Replaying real prices from May 16, 2025, 4:00 pm (Central time) at as fast as the computer can go, using the "
        "AI forecast (TimesFM-3) in Houston and Dallas–Fort Worth and the simple rule in San Antonio and Austin, "
        "whose AI forecasts aren't ready yet.")
    assert event["detail"]["strategy_by_location"] == {"LZ_HOUSTON": "forecast", "LZ_NORTH": "forecast",
                                                       "LZ_CPS": "bands", "LZ_AEN": "bands"}
    assert event["detail"]["fallback_locations"] == ["LZ_CPS", "LZ_AEN"]
    # the parity helper refuses to compare a run that fell back anywhere
    with pytest.raises(RuntimeError, match="fell back"):
        coord.run_fleet_replay("forecast", start_utc="2025-05-16T21:00:00Z", ticks=2, fleet_size=40, workers=1,
                               url=co.url)


def test_battery_status_while_paused_shows_availability_changes_at_once(co):
    from src.sim import battery_status as bs

    co.start({"start_utc": SPIKE_7PM, "speed_tps": "max", "strategy": "peak", "start_charge_level": 0.9},
             max_ticks=40, internal=True)
    assert co.serve_until(lambda: co.run.k >= 3, timeout_s=30)
    co.handle_control({"cmd": "pause"})
    selling = bs.unpack(co.r.get(co.keys.battery_status), co.N)
    assert (selling == bs.SELLING).mean() > 0.9  # 7:15 pm: the evening peak sells everywhere
    co.handle_control({"cmd": "availability", "args": {"scope": "location", "location": "LZ_AEN",
                                                       "offline_fraction": 1.0}})
    aen = co.layout.location_indices("LZ_AEN")
    paused = bs.unpack(co.r.get(co.keys.battery_status), co.N)
    assert (paused[aen] == bs.OFFLINE).all() and np.array_equal(np.delete(paused, aen), np.delete(selling, aen))
    co.handle_control({"cmd": "availability", "args": {"scope": "all", "available": True}})
    back = bs.unpack(co.r.get(co.keys.battery_status), co.N)
    assert (back[aen] == bs.WAITING).all()  # back online; it trades again from the next tick
    co.handle_control({"cmd": "resume"})
    k = co.run.k
    assert co.serve_until(lambda: co.run.k > k, timeout_s=30)
    assert (bs.unpack(co.r.get(co.keys.battery_status), co.N)[aen] == bs.SELLING).mean() > 0.9
