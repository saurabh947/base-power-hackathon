"""The API against a real coordinator + worker processes (small fleet): WebSocket message shapes
exactly as docs/API.md, availability scopes, battery lookup, presenter-only chaos kill, run control."""
import base64
import math
import threading
import time
import uuid
from datetime import datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.api import main
from src.config import load_config
from src.sim import battery_status as bstatus
from src.sim import geo
from src.sim.coordinator import Coordinator
from src.sim.redis_layout import reachable, redis_url

pytestmark = pytest.mark.redis
PASS = "presenter-test-passcode"
LOCS = list(load_config().fleet.locations)

SNAPSHOT_KEYS = {"type", "run_id", "status", "sim_time_utc", "speed_tps", "settings", "history", "totals", "workers",
                 "failed_commands", "availability", "events", "forecast", "battery_status"}
IDLE_KEYS = {"type", "status", "run_id", "last_run"}
FRAME_KEYS = {"type", "run_id", "status", "sim_time_utc", "speed_tps", "ticks", "totals", "workers", "failed_commands",
              "availability", "events", "battery_status"}
TOTALS_KEYS = {"revenue_usd", "sold_kwh", "bought_kwh", "ticks", "by_location"}
TICK_KEYS = {"ts_utc", "ts_local", "price", "decision_price", "action", "target_kw", "actual_kw", "error_pct",
             "revenue_usd", "charge_level", "healthy_workers"}
SETTINGS_KEYS = {"preset", "start_utc", "speed_tps", "strategy", "start_charge_level", "fleet_size", "power_kw_range",
                 "capacity_kwh_range", "locations"}
WORKER_KEYS = {"worker_id", "status", "pid", "batteries", "last_tick_utc", "adopted_from", "by_location"}
EVENT_KEYS = {"id", "wall_utc", "sim_utc", "kind", "severity", "message", "detail"}


@pytest.fixture
def live(monkeypatch):
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    prefix = f"test:api:{uuid.uuid4().hex[:8]}:"
    monkeypatch.setenv("FLEET_FRAME_INTERVAL_S", "0.25")
    monkeypatch.setenv("FLEET_IDLE_INTERVAL_S", "1.0")
    monkeypatch.setenv("FLEET_CONTROL_TIMEOUT_S", "4")
    monkeypatch.setenv("PRESENTER_PASSCODE", PASS)
    monkeypatch.setenv("PRESENTER_PASSCODE_FILE", "/nonexistent/passcode")
    co = Coordinator(url=url, prefix=prefix, fleet_size=300, workers=3)
    co.boot()
    thread = threading.Thread(target=co.serve_forever, daemon=True)
    thread.start()
    main.configure(url=url, prefix=prefix)
    main.state.limits.reset()
    with TestClient(main.app) as client:
        yield client, co
    co.request_stop()
    thread.join(timeout=15)
    co.cleanup_keys()
    main.state.backend = None


def _recv_until(ws, pred, limit=80):
    for _ in range(limit):
        msg = ws.receive_json()
        if pred(msg):
            return msg
    raise AssertionError("expected message never arrived")


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _codes(msg, n=300) -> np.ndarray:
    """battery_status decoded: base64 of packed 4-bit codes, battery i in byte i // 2 (low nibble when even)."""
    raw = base64.b64decode(msg["battery_status"])
    assert len(raw) == math.ceil(n / 2)
    codes = bstatus.unpack(raw, n)
    assert set(codes.tolist()) <= {0, 1, 2, 3, 4}
    return codes


def _assert_common(msg):
    if msg["type"] == "snapshot":
        assert (msg["settings"] is None) if msg["run_id"] is None else (set(msg["settings"]) == SETTINGS_KEYS)
    assert set(msg["totals"]) == TOTALS_KEYS and list(msg["totals"]["by_location"]) == LOCS
    for per in msg["totals"]["by_location"].values():
        assert set(per) == {"revenue_usd", "sold_kwh", "bought_kwh"}
    assert all(set(w) == WORKER_KEYS for w in msg["workers"])
    for w in msg["workers"]:  # batteries each worker owns right now, per city
        assert sum(w["by_location"].values()) == w["batteries"] and set(w["by_location"]) <= set(LOCS)
    _codes(msg)
    assert set(msg["failed_commands"]) == {"count", "recent"} and len(msg["failed_commands"]["recent"]) <= 10
    assert all(set(e) == {"battery_id", "worker_id", "reason", "tick_utc"} for e in msg["failed_commands"]["recent"])
    assert set(msg["availability"]) == {"available", "total", "by_location", "offline_workers"}
    assert all(set(e) == EVENT_KEYS for e in msg["events"])


def _assert_tick(t):
    assert set(t) == TICK_KEYS
    assert t["ts_utc"].endswith("Z") and t["ts_local"][-6:] in ("-05:00", "-06:00")
    for key in ("price", "decision_price", "action"):
        assert set(t[key]) == set(LOCS)
    for key in ("target_kw", "actual_kw", "revenue_usd", "charge_level"):
        assert set(t[key]) == set(LOCS) | {"fleet"}
    assert set(t["action"].values()) <= {"charge", "sell", "hold"}
    assert isinstance(t["error_pct"], float) and isinstance(t["healthy_workers"], int)


def test_websocket_messages_match_the_contract(live):
    client, co = live
    with client.websocket_connect("/live") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and set(snap) == SNAPSHOT_KEYS and snap["status"] == "idle"
        assert snap["run_id"] is None and snap["sim_time_utc"] is None and snap["speed_tps"] == 2
        _assert_common(snap)
        t0 = time.monotonic()
        idle = ws.receive_json()
        assert idle == {"type": "idle", "status": "idle", "run_id": None, "last_run": None}
        assert time.monotonic() - t0 >= 0.7  # idle every FLEET_IDLE_INTERVAL_S (5 s in production), not every frame
        r = client.post("/run/start", json={"preset": "spike_evening", "speed_tps": 10, "strategy": "peak",
                                            "start_charge_level": 0.9, "fleet_size": 5})
        assert r.status_code == 202 and set(r.json()) == {"run_id", "settings"}
        run_id = r.json()["run_id"]
        assert set(r.json()["settings"]) == SETTINGS_KEYS and r.json()["settings"]["fleet_size"] == 300
        fresh = _recv_until(ws, lambda m: m["type"] == "snapshot")  # a new run starts the client over
        assert fresh["run_id"] == run_id and set(fresh) == SNAPSHOT_KEYS and fresh["speed_tps"] == 10
        frames, seen = [], list(fresh["history"])
        while len(seen) < 12:
            f = _recv_until(ws, lambda m: m["type"] == "frame")
            assert set(f) - {"forecast"} == FRAME_KEYS and f["run_id"] == run_id and f["speed_tps"] == 10
            _assert_common(f)
            frames.append(f)
            seen += f["ticks"]
        for t in seen:
            _assert_tick(t)
        stamps = [datetime.fromisoformat(t["ts_utc"].replace("Z", "+00:00")) for t in seen]
        assert stamps[0] == datetime.fromisoformat("2025-05-16T21:00:00+00:00")
        assert all(b - a == timedelta(minutes=5) for a, b in zip(stamps, stamps[1:]))  # every tick, none sampled
        assert any(len(f["ticks"]) > 1 for f in frames)
        assert any(e["kind"] == "run_started" for e in fresh["events"] + sum((f["events"] for f in frames), []))
        state = client.get("/state").json()
        assert set(state) == SNAPSHOT_KEYS and state["run_id"] == run_id and len(state["history"]) >= len(seen)
        assert state["sim_time_utc"].endswith("Z") and state["speed_tps"] == 10
        assert client.get("/health").json()["status"] == "running"
        # a reset ends the run: idle right away, carrying the run that just ended
        assert client.post("/run/reset").json() == {"status": "idle"}
        idle = _recv_until(ws, lambda m: m["type"] == "idle")
        assert set(idle) == IDLE_KEYS and idle["status"] == "idle" and idle["run_id"] is None
        assert idle["last_run"]["run_id"] == run_id and idle["last_run"]["totals"]["ticks"] >= len(seen)
        assert set(idle["last_run"]["totals"]) == TOTALS_KEYS
        per_city = idle["last_run"]["totals"]["by_location"]
        for key in ("revenue_usd", "sold_kwh", "bought_kwh"):  # the cities add up to the fleet
            assert sum(v[key] for v in per_city.values()) == pytest.approx(idle["last_run"]["totals"][key], abs=1e-4)


def test_forecasts_ride_along_only_when_new(live, monkeypatch):
    import sys
    import types

    from src.api import live as live_mod

    client, co = live
    provider = types.ModuleType("src.forecast.provider")
    calls = []

    def latest_forecast(location, at_utc):  # synthetic test double: one forecast per sim hour
        calls.append((location, at_utc))
        issued = at_utc.replace(minute=0, second=0, microsecond=0)
        return {"location": location, "series": "rt_price", "model": "timesfm-3.0", "issued_utc": issued,
                "step_minutes": 15, "points": [{"ts_utc": issued, "p10": 1.0, "p50": 2.0, "p90": 3.0}]}

    provider.latest_forecast = latest_forecast
    monkeypatch.setitem(sys.modules, "src.forecast.provider", provider)
    monkeypatch.setattr(live_mod, "forecasts", live_mod.ForecastCache())
    client.post("/run/start", json={"speed_tps": 10, "strategy": "peak"})
    assert _wait(lambda: co.run is not None and co.run.k >= 2)
    with client.websocket_connect("/live") as ws:
        snap = ws.receive_json()
        assert set(snap["forecast"]) == set(LOCS)
        assert snap["forecast"]["LZ_NORTH"]["issued_utc"].endswith("Z")  # datetimes serialised as *_utc strings
        with_forecast = []
        for _ in range(12):
            f = ws.receive_json()
            if "forecast" in f:
                with_forecast.append(f["forecast"]["LZ_HOUSTON"]["issued_utc"])
        assert with_forecast and len(set(with_forecast)) == len(with_forecast)  # only when a new one was issued
    assert _wait(lambda: any(e["kind"] == "forecast_issued" for e in client.get("/state").json()["events"]))


def test_run_controls(live):
    client, co = live
    for path, body in (("/run/pause", None), ("/run/resume", None), ("/run/speed", {"speed_tps": 5})):
        r = client.post(path, json=body)  # nothing to control yet
        assert r.status_code == 409 and r.json()["error"] == "no_active_run", path
    run_id = client.post("/run/start", json={"speed_tps": 10, "strategy": "bands"}).json()["run_id"]
    assert _wait(lambda: co.run is not None and co.run.k >= 2)
    assert client.post("/run/pause").json() == {"status": "paused"}
    k = co.run.k
    time.sleep(0.4)
    assert co.run.k == k and client.get("/health").json()["status"] == "paused"
    assert client.post("/run/resume").json() == {"status": "running"}
    with client.websocket_connect("/live") as ws:
        assert ws.receive_json()["speed_tps"] == 10
        assert client.post("/run/speed", json={"speed_tps": 5}).json() == {"speed_tps": 5}
        frame = _recv_until(ws, lambda m: m["type"] == "frame")  # the very next frame shows the new speed
        assert frame["speed_tps"] == 5
    state = client.get("/state").json()
    assert state["speed_tps"] == 5 and state["settings"]["speed_tps"] == 5
    assert _wait(lambda: co.run.k > k)
    assert co.run.run_id == run_id and co.run.speed == 5
    kinds = [e["kind"] for e in co.events]
    assert {"run_started", "run_paused", "run_resumed", "speed_changed"} <= set(kinds)
    assert client.post("/run/reset").json() == {"status": "idle"}
    assert _wait(lambda: co.status == "idle" and co.run is None)
    assert client.post("/run/reset").status_code == 429  # one per 5 s


def test_availability_scopes(live):
    client, co = live
    client.post("/run/start", json={"speed_tps": 10, "strategy": "peak"})
    assert _wait(lambda: co.run is not None and co.status == "running")
    houston = co.layout.counts[0]
    off = round(0.3 * houston)
    r = client.post("/availability", json={"scope": "location", "location": "LZ_HOUSTON", "offline_fraction": 0.3})
    body = r.json()
    assert r.status_code == 200 and set(body) == {"applied_from_tick_utc", "affected", "available", "total"}
    assert (body["affected"], body["available"], body["total"]) == (off, 300 - off, 300)
    assert body["applied_from_tick_utc"].endswith("Z")
    again = client.post("/availability", json={"scope": "location", "location": "LZ_HOUSTON", "offline_fraction": 0.3})
    assert again.json()["affected"] == 0  # the same fraction picks the same batteries
    mine = co.owner == 1
    assert mine.sum() == 100 and np.array_equal(np.flatnonzero(mine), np.arange(100, 200))  # a contiguous block
    already_off = int(co.availability.offline[mine].sum())
    r = client.post("/availability", json={"scope": "worker", "worker_id": 1, "available": False})
    assert r.json()["available"] == 300 - off - (100 - already_off) and r.json()["affected"] == 100 - already_off
    snap = client.get("/state").json()
    assert snap["availability"]["offline_workers"] == [1]
    north = co.layout.location_indices("LZ_NORTH")
    assert snap["availability"]["by_location"]["LZ_NORTH"] == {
        "available": int(len(north) - mine[north].sum()), "total": int(len(north))}
    r = client.post("/availability", json={"scope": "battery", "battery_id": "b00299", "available": False})
    assert r.json()["affected"] == 1
    assert client.get("/batteries/b00299").json()["available"] is False
    r = client.post("/availability", json={"scope": "battery", "battery_id": "b99999", "available": False})
    assert r.status_code == 404 and r.json()["error"] == "unknown_battery"
    r = client.post("/availability", json={"scope": "worker", "worker_id": 99, "available": False})
    assert r.status_code == 404
    r = client.post("/availability", json={"scope": "all", "available": True})
    assert r.json()["available"] == 300
    # offline batteries got 0: they never moved while offline, the others did
    assert _wait(lambda: any(e["kind"] == "availability_changed" for e in co.events))
    msgs = [e["message"] for e in co.events if e["kind"] == "availability_changed"]
    assert f"30% of the Houston batteries ({off}) are now offline; {300 - off} of 300 batteries are online." in msgs


def test_battery_lookup(live):
    client, co = live
    b = client.get("/batteries/b00007").json()
    assert set(b) == {"battery_id", "location", "city", "lonlat", "worker_id", "capacity_kwh", "power_kw",
                      "charge_level", "power_kw_now", "available"}
    assert b["battery_id"] == "b00007" and b["location"] == "LZ_HOUSTON" and b["worker_id"] == 0
    assert b["city"] == "Houston" and b["available"] is True
    assert b["capacity_kwh"] == pytest.approx(float(co.cap[7])) and b["power_kw"] == pytest.approx(float(co.power[7]))
    layout = client.get("/fleet/layout").json()
    pos = geo.decode_positions(base64.b64decode(layout["positions"]))
    assert b["lonlat"] == pytest.approx(pos[7].tolist(), abs=1e-9)  # exactly where the map draws it
    for city in layout["cities"][1:]:  # the first battery of every other city (contiguous blocks in config order)
        g = city["first_battery"]
        got = client.get(f"/batteries/b{g:05d}").json()
        assert (got["location"], got["city"]) == (city["location"], city["name"])
        assert got["worker_id"] == int(co.owner[g]) and got["lonlat"] == pytest.approx(pos[g].tolist(), abs=1e-9)
    assert client.get("/batteries/b00300").status_code == 404
    assert client.get("/batteries/LZ_HOUSTON-000007").status_code == 404
    assert client.get("/batteries/garbage").status_code == 404
    assert client.get("/settings").json()["defaults"]["fleet_size"] == 300  # the fleet that is actually running


def test_presenter_kill_flow(live):
    client, co = live
    client.post("/run/start", json={"speed_tps": 10, "strategy": "peak", "start_charge_level": 0.9})
    assert _wait(lambda: co.run is not None and co.status == "running")
    assert client.post("/chaos/kill?worker_id=1").status_code == 403
    hdr = {"X-Presenter-Passcode": PASS}
    r = client.post("/chaos/kill?worker_id=1", headers=hdr)
    assert r.status_code == 200 and set(r.json()) == {"worker_id", "pid", "killed_at_utc"}
    assert r.json()["worker_id"] == 1 and r.json()["pid"] == co.workers[1].pid
    # before the coordinator even notices, the kill already counts: no double kill, no dropping below 2
    assert client.post("/chaos/kill?worker_id=1", headers=hdr).json()["error"] == "already_dead"
    assert client.post("/chaos/kill?worker_id=2", headers=hdr).json()["error"] == "too_few_healthy"
    assert _wait(lambda: co.workers[1].status == "dead", timeout=10)
    r = client.post("/chaos/kill?worker_id=1", headers=hdr)
    assert r.status_code == 409 and r.json()["error"] == "already_dead"
    r = client.post("/chaos/kill", headers=hdr)  # random healthy one: would leave 1 healthy
    assert r.status_code == 409 and r.json()["error"] == "too_few_healthy"
    r = client.post("/chaos/kill?worker_id=42", headers=hdr)
    assert r.status_code == 404
    snap = client.get("/state").json()
    assert [w["status"] for w in snap["workers"]] == ["healthy", "dead", "healthy"]
    assert snap["failed_commands"]["count"] > 0
    assert {e["reason"] for e in snap["failed_commands"]["recent"]} == {"expired_owner_dead"}
    assert sum(w["batteries"] for w in snap["workers"]) == 300 and snap["workers"][1]["batteries"] == 0
    assert sorted(w["adopted_from"] for w in snap["workers"] if w["worker_id"] != 1) == [[1], [1]]
    kinds = [e["kind"] for e in snap["events"]]
    assert "worker_dead" in kinds and "worker_adopted" in kinds
    adopted = next(e for e in snap["events"] if e["kind"] == "worker_adopted")
    assert adopted["detail"]["worker_id"] == 1 and adopted["detail"]["controller"] == 2
    assert sorted(adopted["detail"]["adopted_by"]) == [0, 2] and adopted["detail"]["ms"] >= 0
    cities = geo.join_names([geo.city_name(loc) for loc in co.layout.counts_by_location(np.arange(100, 200))])
    assert adopted["message"].startswith(f"Controller 2's 100 batteries in {cities} were handed to the other 2 "
                                         "controllers")
    assert adopted["detail"]["by_location"] == co.layout.counts_by_location(np.arange(100, 200))
    # after the hand-over the survivors' by_location includes the adopted batteries
    assert sum(sum(w["by_location"].values()) for w in snap["workers"]) == 300
    assert snap["workers"][1]["by_location"] == {}
    # the coordinator, the API and Redis are never valid targets
    assert co.workers[1].exit_code == -9


def test_fleet_layout_is_the_running_fleet(live):
    client, co = live
    r = client.get("/fleet/layout")
    assert r.status_code == 200 and r.headers["etag"] == f'"{r.json()["version"]}"'
    layout = r.json()
    assert set(layout) == {"version", "fleet_size", "cities", "positions"} and layout["fleet_size"] == 300
    assert [c["location"] for c in layout["cities"]] == list(co.layout.locations)
    assert [c["batteries"] for c in layout["cities"]] == list(co.layout.counts)
    assert [c["first_battery"] for c in layout["cities"]] == co.layout.offsets
    pos = geo.decode_positions(base64.b64decode(layout["positions"]))
    assert pos.shape == (300, 2)
    for c in layout["cities"]:
        city = geo.city(c["location"])
        block = pos[c["first_battery"]:c["first_battery"] + c["batteries"]]
        assert (geo.distance_km(city, block[:, 0], block[:, 1]) <= city.radius_km + 0.2).all()
    again = client.get("/fleet/layout", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.headers["etag"] == r.headers["etag"]


def test_battery_status_idle_follows_availability_and_a_run_shows_what_batteries_do(live):
    client, co = live
    snap = client.get("/state").json()
    assert snap["run_id"] is None and set(_codes(snap).tolist()) == {bstatus.WAITING}  # no run: all waiting
    client.post("/availability", json={"scope": "location", "location": "LZ_CPS", "offline_fraction": 1.0})
    codes = _codes(client.get("/state").json())  # no run: offline shows at once
    cps = co.layout.location_indices("LZ_CPS")
    assert (codes[cps] == bstatus.OFFLINE).all() and (np.delete(codes, cps) == bstatus.WAITING).all()
    client.post("/run/start", json={"preset": "spike_evening", "speed_tps": 10, "strategy": "peak",
                                    "start_charge_level": 0.9})
    assert _wait(lambda: co.run is not None and co.run.k >= 30, timeout=20)  # after 6 pm: the peak rule sells
    with client.websocket_connect("/live") as ws:
        first = ws.receive_json()
        frame = _recv_until(ws, lambda m: m["type"] == "frame")
    for msg in (first, frame):
        codes = _codes(msg)
        assert (codes[cps] == bstatus.OFFLINE).all()
        others = np.delete(codes, cps)
        assert set(others.tolist()) <= {bstatus.WAITING, bstatus.SELLING, bstatus.CHARGING}
        assert (others == bstatus.SELLING).sum() > 0.5 * len(others)  # the evening peak sells
    totals = frame["totals"]
    assert totals["by_location"]["LZ_CPS"] == {"revenue_usd": 0.0, "sold_kwh": 0.0, "bought_kwh": 0.0}  # offline
    for key in ("revenue_usd", "sold_kwh", "bought_kwh"):
        assert sum(v[key] for v in totals["by_location"].values()) == pytest.approx(totals[key], abs=1e-4)
    assert totals["by_location"]["LZ_HOUSTON"]["sold_kwh"] > 0


def test_a_start_right_after_a_reset_waits_for_the_controllers(live, monkeypatch):
    """Cloud Run logged Starts failing with 503 after ~4 s while a Reset respawned the 20 workers: the Start
    sat in the control stream behind the blocking respawn. Now /run/start waits until the coordinator says it
    is ready, then starts. A short control timeout makes the old failure certain here."""
    client, co = live
    assert client.post("/run/start", json={"speed_tps": 10, "strategy": "peak"}).status_code == 202
    assert _wait(lambda: co.run is not None and co.status == "running")
    respawn = co.reset

    def slow_reset():  # respawning 20 workers on 4 vCPUs takes seconds; 3 on a laptop take ~0.1 s
        time.sleep(1.0)
        respawn()

    monkeypatch.setattr(co, "reset", slow_reset)
    monkeypatch.setenv("FLEET_CONTROL_TIMEOUT_S", "0.5")  # the API's control timeout, shorter than the respawn
    main.state.limits.run_start.reset()  # the next Start comes at once, as a visitor's Reset-then-Start does
    t0 = time.monotonic()
    assert client.post("/run/reset").json() == {"status": "idle"}
    r = client.post("/run/start", json={"speed_tps": 10, "strategy": "bands"})
    waited = time.monotonic() - t0
    assert r.status_code == 202, r.json()
    assert _wait(lambda: co.run is not None and co.run.run_id == r.json()["run_id"] and co.status == "running")
    assert all(h.status == "healthy" for h in co.workers)
    print(f"\nreset -> start accepted in {waited:.2f}s")
