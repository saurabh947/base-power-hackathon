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
    # docs/API.md item shapes
    assert all(set(p) == {"id", "label", "date_local", "start_utc", "start_local", "description"} for p in s["presets"])
    first = s["presets"][0]
    assert (first["id"], first["start_utc"], first["start_local"]) == (
        "spike_evening", "2025-05-16T21:00:00Z", "2025-05-16T16:00:00-05:00")
    assert s["strategies"] == [
        {"id": "bands", "label": "Simple rule", "description": s["strategies"][0]["description"]},
        {"id": "forecast", "label": "AI forecast (TimesFM-3)", "description": s["strategies"][1]["description"]},
        {"id": "peak", "label": "Evening peak", "description": s["strategies"][2]["description"]},
    ]
    assert s["speeds"] == [{"tps": 1, "label": "1 hour every 12 s"}, {"tps": 2, "label": "1 hour every 6 s"},
                           {"tps": 5, "label": "1 hour every 2.4 s"}, {"tps": 10, "label": "1 hour every 1.2 s"}]


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
        allowed = {h.strip().lower() for h in r.headers["access-control-allow-headers"].split(",")}
        assert {"content-type", "x-presenter-passcode"} <= allowed
    r = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers["access-control-expose-headers"].lower() == "retry-after"
    r = client.options("/run/start", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "x-evil-header"})
    assert r.status_code == 400  # only the two pinned request headers are allowed
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


def _fake_service(results: dict | None):
    """A stand-in for src.backtest.service with its documented error contract (test double)."""
    svc = types.ModuleType("src.backtest.service")
    jobs = {}

    class ResultsNotFound(LookupError):
        pass

    def start_backtest(strategies, year):
        if not strategies or any(s not in ("bands", "forecast", "peak") for s in strategies):
            raise ValueError("Unknown decision method 'solver'. Choose from: bands, forecast, peak.")
        for job_id, job in jobs.items():  # one job at a time: hand back the running one
            if job["status"] == "running":
                return job_id
        jobs["job-1"] = {"status": "running", "run_ids": [], "error": None, "strategies": strategies, "year": year}
        return "job-1"

    def get_job(job_id):
        job = jobs[job_id]  # KeyError for unknown ids
        return {"status": job["status"], "run_ids": job["run_ids"], "error": job["error"]}

    def pitch_results(year=None):
        if results is None:
            raise ResultsNotFound("There are no full-year results for 2025 yet.")
        return results

    svc.ResultsNotFound, svc.start_backtest, svc.get_job, svc.pitch_results = ResultsNotFound, start_backtest, get_job, pitch_results
    svc.add_listener = lambda fn: None
    svc.jobs = jobs
    return svc


def test_bridges_call_the_parallel_modules(client, monkeypatch):
    svc = _fake_service({"year": 2025, "strategies": []})
    provider = types.ModuleType("src.forecast.provider")
    calls = []

    def latest_forecast(location, at, series="rt_price"):
        calls.append((location, series))
        if location == "LZ_HOUSTON" and at.year == 2020:
            return None
        return {"location": location, "series": series, "issued_utc": at, "points": []}

    provider.latest_forecast = latest_forecast
    monkeypatch.setitem(sys.modules, "src.backtest.service", svc)
    monkeypatch.setitem(sys.modules, "src.forecast.provider", provider)
    r = client.post("/backtest", json={"strategies": ["bands", "peak"], "year": 2025})
    assert r.status_code == 202 and r.json() == {"job_id": "job-1"}
    assert client.post("/backtest", json={"strategies": ["bands"], "year": 2025}).json() == {"job_id": "job-1"}
    assert client.get("/backtest/job-1").json() == {"status": "running", "run_ids": [], "skipped": {}, "error": None}
    r = client.get("/backtest/nope")
    assert r.status_code == 404 and r.json()["error"] == "unknown_job"
    r = client.post("/backtest", json={"strategies": ["solver"]})
    assert r.status_code == 400 and r.json()["message"].startswith("Unknown decision method")
    assert client.get("/results/pitch").json()["year"] == 2025
    r = client.get("/forecast", params={"location": "LZ_NORTH", "at_utc": "2025-05-16T21:00:00Z"})
    assert r.status_code == 200 and r.json()["location"] == "LZ_NORTH"
    assert r.json()["issued_utc"] == "2025-05-16T21:00:00Z"  # datetimes serialised as *_utc strings
    r = client.get("/forecast", params={"location": "ERCOT", "series": "demand", "at_utc": "2025-05-16T21:00:00Z"})
    assert r.status_code == 200 and calls[-1] == ("ERCOT", "demand")
    r = client.get("/forecast", params={"location": "LZ_HOUSTON", "at_utc": "2020-01-01T00:00:00Z"})
    assert r.status_code == 404 and r.json()["error"] == "no_forecast"
    assert client.get("/forecast", params={"location": "LZ_WEST"}).status_code == 400
    assert client.get("/forecast", params={"location": "LZ_NORTH", "series": "demand"}).status_code == 400
    assert client.get("/forecast", params={"location": "LZ_NORTH", "series": "wind"}).status_code == 400


def test_results_before_any_full_year_run_is_404(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "src.backtest.service", _fake_service(None))
    r = client.get("/results/pitch")
    assert r.status_code == 404 and r.json() == {"error": "no_results", "message": "No full-year results yet."}


def test_the_real_backtest_service_is_bridged(client):
    """src.backtest.service from main: unknown job 404, bad input 400, no results yet 404."""
    from src.backtest import service

    assert client.get("/backtest/not-a-job").status_code == 404
    r = client.post("/backtest", json={"strategies": ["solver"], "year": 2025})
    assert r.status_code == 400 and r.json()["error"] == "invalid_backtest"
    r = client.post("/backtest", json={"strategies": ["bands"], "year": 1999})
    assert r.status_code == 400
    assert main._backtest_listener in service._listeners  # backtest_done comes from the service's hook
