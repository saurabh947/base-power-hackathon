"""API behaviour that needs no services: settings, errors, presenter auth, rate limits, CORS,
and the 501 bridges to modules built in parallel. Redis is pointed at a closed port."""
import sys
import types

import pytest
from fastapi.testclient import TestClient

from src.api import main
from src.api.limits import SlidingWindow

PASS = "correct horse battery staple"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PRESENTER_PASSCODE_FILE", str(tmp_path / "missing"))
    monkeypatch.delenv("PRESENTER_PASSCODE", raising=False)
    monkeypatch.delenv("DEMO_PRESENTER_PASSCODE", raising=False)
    monkeypatch.setenv("FLEET_CONTROL_TIMEOUT_S", "0.2")
    main.configure(url="redis://127.0.0.1:1/0", prefix="test:unit:")  # nothing listens on port 1
    main.state.limits.reset()
    with TestClient(main.app) as c:
        yield c
    main.state.backend = None


def test_health_and_settings(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["status"] == "idle"
    s = client.get("/settings").json()
    assert set(s) == {"defaults", "editable", "presets", "strategies", "speeds"}
    assert s["editable"] == ["preset", "speed_tps", "strategy", "start_charge_level"]
    assert s["defaults"]["fleet_size"] == 20000 and s["defaults"]["speed_tps"] == 2
    assert [p["id"] for p in s["presets"]][0] == "spike_evening"
    assert [c["value"] for c in s["speeds"]] == [1, 2, 5, 10]
    assert {c["value"] for c in s["strategies"]} == {"bands", "forecast", "peak"}
    assert all({"value", "label"} <= set(c) for c in s["speeds"] + s["strategies"])


def test_errors_are_plain_json(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.json()["error"] == "not_found" and r.json()["message"]
    r = client.post("/run/start", content="{not json")
    assert r.status_code == 400 and r.json()["error"] == "invalid_json"
    r = client.post("/run/start", json={"preset": "no-such-day"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_preset"
    r = client.post("/run/speed", json={"speed_tps": 7})
    assert r.status_code == 400 and r.json()["error"] == "invalid_speed"
    r = client.post("/availability", json={"scope": "planet"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope"
    r = client.post("/chaos/kill")
    assert r.status_code == 503  # no passcode configured (checked before anything else)


def test_simulator_unreachable_is_a_503(client):
    r = client.post("/run/pause")
    assert r.status_code == 503 and r.json()["error"] == "simulator_unavailable"


def test_presenter_passcode_not_set(client):
    for path in ("/presenter/check", "/chaos/kill"):
        r = client.post(path, headers={"X-Presenter-Passcode": "anything"})
        assert r.status_code == 503 and r.json()["error"] == "presenter_passcode_not_set"


def test_presenter_passcode_from_env_and_file(client, monkeypatch, tmp_path):
    monkeypatch.setenv("PRESENTER_PASSCODE", PASS)
    assert client.post("/presenter/check", headers={"X-Presenter-Passcode": PASS}).json() == {"ok": True}
    r = client.post("/presenter/check", headers={"X-Presenter-Passcode": PASS + "x"})
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert client.post("/presenter/check").status_code == 403
    secret = tmp_path / "passcode"
    secret.write_text("from-the-secret-file\n")
    monkeypatch.setenv("PRESENTER_PASSCODE_FILE", str(secret))  # read at request time; the file wins
    assert client.post("/presenter/check", headers={"X-Presenter-Passcode": "from-the-secret-file"}).status_code == 200
    assert client.post("/presenter/check", headers={"X-Presenter-Passcode": PASS}).status_code == 403


def test_wrong_passcodes_are_limited_per_ip(client, monkeypatch):
    monkeypatch.setenv("PRESENTER_PASSCODE", PASS)
    for _ in range(5):
        assert client.post("/chaos/kill", headers={"X-Presenter-Passcode": "guess"}).status_code == 403
    r = client.post("/presenter/check", headers={"X-Presenter-Passcode": PASS})
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert 1 <= r.json()["retry_after_s"] <= 60 and r.headers["Retry-After"] == str(r.json()["retry_after_s"])
    main.state.limits.wrong_passcode.reset()
    assert client.post("/presenter/check", headers={"X-Presenter-Passcode": PASS}).status_code == 200


def test_run_start_and_reset_are_limited_to_one_per_5s(client):
    first = client.post("/run/start", json={"speed_tps": 2})
    assert first.status_code == 503  # counted, then the (absent) simulator didn't answer
    second = client.post("/run/start", json={"speed_tps": 2})
    assert second.status_code == 429 and second.json()["error"] == "rate_limited"
    assert second.json()["retry_after_s"] in (4, 5)
    assert client.post("/run/start", json={"preset": "bad"}).status_code == 400  # invalid calls don't wait
    assert client.post("/run/reset").status_code == 503
    assert client.post("/run/reset").status_code == 429


def test_availability_is_limited_to_10_per_second(client):
    codes = [client.post("/availability", json={"scope": "all", "available": True}).status_code for _ in range(12)]
    assert codes[:10] == [503] * 10 and codes[10:] == [429, 429]


def test_sliding_window_frees_up():
    now = [100.0]
    lim = SlidingWindow(2, 1.0, clock=lambda: now[0])
    assert lim.hit() is None and lim.hit() is None and lim.hit() == pytest.approx(1.0)
    now[0] += 0.5
    assert lim.hit() == pytest.approx(0.5)
    now[0] += 0.51
    assert lim.hit() is None


def test_cors(client):
    for origin in ("https://fleet-sim.vercel.app", "https://fleet-sim-git-main-me.vercel.app", "http://localhost:5173"):
        r = client.options("/run/start", headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                   "Access-Control-Request-Headers": "x-presenter-passcode,content-type"})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origin
    r = client.options("/run/start", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
    r = client.get("/health", headers={"Origin": "https://vercel.app.evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_bridges_answer_501_until_their_modules_land(client, monkeypatch):
    real_import = main.importlib.import_module

    def missing(name, *a, **kw):
        if name in ("src.backtest.service", "src.forecast.provider"):
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(main.importlib, "import_module", missing)
    for method, path, body in (("post", "/backtest", {"strategies": ["bands"], "year": 2025}),
                               ("get", "/backtest/abc", None), ("get", "/results/pitch", None),
                               ("get", "/forecast?location=LZ_HOUSTON&at_utc=2025-05-16T21:00:00Z", None)):
        r = getattr(client, method)(path, **({"json": body} if body else {}))
        assert r.status_code == 501 and r.json()["error"] == "not_ready", path


def test_bridges_call_the_parallel_modules(client, monkeypatch):
    svc = types.ModuleType("src.backtest.service")
    jobs = {}
    svc.start_backtest = lambda strategies, year: jobs.setdefault("job-1", {"strategies": strategies, "year": year}) and "job-1"
    svc.get_job = lambda job_id: {"status": "running", "run_ids": [], "error": None} if job_id in jobs else None
    svc.pitch_results = lambda: {"year": 2025, "strategies": []}
    provider = types.ModuleType("src.forecast.provider")
    provider.latest_forecast = lambda location, at: {"location": location, "issued_utc": at.isoformat(), "points": []}
    monkeypatch.setitem(sys.modules, "src.backtest.service", svc)
    monkeypatch.setitem(sys.modules, "src.forecast.provider", provider)
    r = client.post("/backtest", json={"strategies": ["bands", "peak"], "year": 2025})
    assert r.status_code == 202 and r.json() == {"job_id": "job-1"}
    assert jobs["job-1"] == {"strategies": ["bands", "peak"], "year": 2025}
    assert client.get("/backtest/job-1").json()["status"] == "running"
    assert client.get("/backtest/nope").status_code == 404
    assert client.get("/results/pitch").json()["year"] == 2025
    r = client.get("/forecast", params={"location": "LZ_NORTH", "at_utc": "2025-05-16T21:00:00Z"})
    assert r.status_code == 200 and r.json()["location"] == "LZ_NORTH"
    assert client.get("/forecast", params={"location": "LZ_WEST"}).status_code == 400
    assert client.post("/backtest", json={"strategies": ["solver"]}).status_code == 400
