"""The API against a real coordinator + worker processes (small fleet): WebSocket message shapes
exactly as docs/API.md, availability scopes, battery lookup, presenter-only chaos kill, run control."""
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api import main
from src.sim.coordinator import Coordinator
from src.sim.redis_layout import reachable, redis_url

pytestmark = pytest.mark.redis
PASS = "presenter-test-passcode"

SNAPSHOT_KEYS = {"type", "run_id", "status", "settings", "history", "totals", "workers", "failed_commands",
                 "availability", "events", "forecast"}
FRAME_KEYS = {"type", "run_id", "status", "sim_time_utc", "speed_tps", "ticks", "totals", "workers", "failed_commands",
              "availability", "events"}
TICK_KEYS = {"ts_utc", "ts_local", "price", "decision_price", "action", "target_kw", "actual_kw", "error_pct",
             "revenue_usd", "charge_level", "healthy_workers"}
SETTINGS_KEYS = {"preset", "start_utc", "speed_tps", "strategy", "start_charge_level", "fleet_size", "power_kw_range",
                 "capacity_kwh_range", "locations"}
WORKER_KEYS = {"worker_id", "status", "pid", "batteries", "last_tick_utc", "adopted_from"}
EVENT_KEYS = {"id", "wall_utc", "sim_utc", "kind", "severity", "message", "detail"}


@pytest.fixture
def live(monkeypatch):
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    prefix = f"test:api:{uuid.uuid4().hex[:8]}:"
    monkeypatch.setenv("FLEET_FRAME_INTERVAL_S", "0.25")
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


def _assert_common(msg):
    if msg["type"] == "snapshot":
        assert set(msg["settings"]) == SETTINGS_KEYS
    assert set(msg["totals"]) == {"revenue_usd", "sold_kwh", "bought_kwh", "ticks"}
    assert all(set(w) == WORKER_KEYS for w in msg["workers"])
    assert set(msg["failed_commands"]) == {"count", "recent"} and len(msg["failed_commands"]["recent"]) <= 10
    assert all(set(e) == {"battery_id", "worker_id", "reason", "tick_utc"} for e in msg["failed_commands"]["recent"])
    assert set(msg["availability"]) == {"available", "total", "by_location", "offline_workers"}
    assert all(set(e) == EVENT_KEYS for e in msg["events"])


def _assert_tick(t):
    assert set(t) == TICK_KEYS
    assert t["ts_utc"].endswith("Z") and t["ts_local"][-6:] in ("-05:00", "-06:00")
    for key in ("price", "decision_price", "action"):
        assert set(t[key]) == {"LZ_HOUSTON", "LZ_NORTH"}
    for key in ("target_kw", "actual_kw", "revenue_usd", "charge_level"):
        assert set(t[key]) == {"LZ_HOUSTON", "LZ_NORTH", "fleet"}
    assert set(t["action"].values()) <= {"charge", "sell", "hold"}
    assert isinstance(t["error_pct"], float) and isinstance(t["healthy_workers"], int)


def test_websocket_messages_match_the_contract(live):
    client, co = live
    with client.websocket_connect("/live") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and set(snap) == SNAPSHOT_KEYS and snap["status"] == "idle"
        _assert_common(snap)
        idle = ws.receive_json()
        assert idle["type"] == "idle" and set(idle) == FRAME_KEYS and idle["ticks"] == []
        r = client.post("/run/start", json={"preset": "spike_evening", "speed_tps": 10, "strategy": "peak",
                                            "start_charge_level": 0.9, "fleet_size": 5})
        assert r.status_code == 202 and set(r.json()) == {"run_id", "settings"}
        run_id = r.json()["run_id"]
        assert set(r.json()["settings"]) == SETTINGS_KEYS and r.json()["settings"]["fleet_size"] == 300
        fresh = _recv_until(ws, lambda m: m["type"] == "snapshot")  # a new run starts the client over
        assert fresh["run_id"] == run_id and set(fresh) == SNAPSHOT_KEYS
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
        assert client.get("/health").json()["status"] == "running"


def test_run_controls(live):
    client, co = live
    assert client.post("/run/pause").status_code == 409  # nothing to pause yet
    run_id = client.post("/run/start", json={"speed_tps": 10, "strategy": "bands"}).json()["run_id"]
    assert _wait(lambda: co.run is not None and co.run.k >= 2)
    assert client.post("/run/pause").json() == {"status": "paused"}
    k = co.run.k
    time.sleep(0.4)
    assert co.run.k == k and client.get("/health").json()["status"] == "paused"
    assert client.post("/run/resume").json() == {"status": "running"}
    assert client.post("/run/speed", json={"speed_tps": 5}).json() == {"speed_tps": 5}
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
    r = client.post("/availability", json={"scope": "location", "location": "LZ_HOUSTON", "offline_fraction": 0.3})
    body = r.json()
    assert r.status_code == 200 and set(body) == {"applied_from_tick_utc", "affected", "available", "total"}
    assert (body["affected"], body["available"], body["total"]) == (45, 255, 300)
    assert body["applied_from_tick_utc"].endswith("Z")
    again = client.post("/availability", json={"scope": "location", "location": "LZ_HOUSTON", "offline_fraction": 0.3})
    assert again.json()["affected"] == 0  # the same fraction picks the same batteries
    mine = co.owner == 1
    assert mine.sum() == 100  # 50 Houston + 50 Dallas-Fort Worth
    already_off = int(co.availability.offline[mine].sum())
    r = client.post("/availability", json={"scope": "worker", "worker_id": 1, "available": False})
    assert r.json()["available"] == 255 - (100 - already_off) and r.json()["affected"] == 100 - already_off
    snap = client.get("/state").json()
    assert snap["availability"]["offline_workers"] == [1]
    assert snap["availability"]["by_location"]["LZ_NORTH"] == {"available": 100, "total": 150}
    r = client.post("/availability", json={"scope": "battery", "battery_id": "LZ_NORTH-000149", "available": False})
    assert r.json()["affected"] == 1
    assert client.get("/batteries/LZ_NORTH-000149").json()["available"] is False
    r = client.post("/availability", json={"scope": "battery", "battery_id": "LZ_NORTH-999999", "available": False})
    assert r.status_code == 404 and r.json()["error"] == "unknown_battery"
    r = client.post("/availability", json={"scope": "worker", "worker_id": 99, "available": False})
    assert r.status_code == 404
    r = client.post("/availability", json={"scope": "all", "available": True})
    assert r.json()["available"] == 300
    # offline batteries got 0: they never moved while offline, the others did
    assert _wait(lambda: any(e["kind"] == "availability_changed" for e in co.events))
    msgs = [e["message"] for e in co.events if e["kind"] == "availability_changed"]
    assert any("Houston area" in m and "offline" in m for m in msgs)


def test_battery_lookup(live):
    client, co = live
    b = client.get("/batteries/LZ_HOUSTON-000007").json()
    assert set(b) == {"battery_id", "location", "worker_id", "capacity_kwh", "power_kw", "charge_level",
                      "power_kw_now", "available"}
    assert b["location"] == "LZ_HOUSTON" and b["worker_id"] == 0 and b["available"] is True
    assert b["capacity_kwh"] == pytest.approx(float(co.cap[7])) and b["power_kw"] == pytest.approx(float(co.power[7]))
    assert client.get("/batteries/LZ_HOUSTON-000150").status_code == 404
    assert client.get("/batteries/garbage").status_code == 404


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
    # the coordinator, the API and Redis are never valid targets
    assert co.workers[1].exit_code == -9
