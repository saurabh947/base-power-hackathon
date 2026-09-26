"""API behaviour that needs no services: settings, errors, rate limits (crashing a controller included), CORS,
and the 501 bridges to modules built in parallel. Redis is pointed at a closed port."""
import sys
import types

import pytest
from fastapi.testclient import TestClient

from src.api import main
from src.api.limits import SlidingWindow


@pytest.fixture
def client(monkeypatch, tmp_path):
    from src.data import years
    from src.forecast import predictions

    monkeypatch.setenv("FLEET_CONTROL_TIMEOUT_S", "0.2")
    monkeypatch.setenv("FORECAST_SOURCE", "parquet")  # predictions from an empty folder: none made yet
    monkeypatch.delenv("PRICE_SOURCE", raising=False)  # real prices: the committed snapshot
    monkeypatch.setattr(predictions, "default_cache_dir", lambda: tmp_path / "predictions")
    years.clear_cache()
    predictions.clear_cache()
    main.configure(url="redis://127.0.0.1:1/0", prefix="test:unit:")  # nothing listens on port 1
    main.state.limits.reset()
    with TestClient(main.app) as c:
        yield c
    main.state.backend = None


def test_health_and_settings(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["status"] == "idle"
    s = client.get("/settings").json()
    assert set(s) == {"defaults", "editable", "presets", "strategies", "speeds", "years"}
    assert s["editable"] == ["year", "preset", "speed_tps", "strategy", "start_charge_level"]
    assert s["defaults"]["fleet_size"] == 20000 and s["defaults"]["speed_tps"] == 2
    assert (s["defaults"]["year"], s["defaults"]["preset"]) == (2025, "ai_best_evening")
    # docs/API.md item shapes
    assert all(set(p) == {"id", "year", "label", "date_local", "start_utc", "start_local", "description"}
               for p in s["presets"])
    first = s["presets"][0]
    assert (first["id"], first["year"], first["start_utc"], first["start_local"]) == (
        "spike_evening", 2025, "2025-05-16T21:00:00Z", "2025-05-16T16:00:00-05:00")
    # years: 2025 and 2026, no 2024; 2026's real prices end where the committed 2026 snapshot does (files of Sep 20)
    assert s["years"] == [
        {"year": 2025, "label": "2025", "description": "Real ERCOT prices for the whole year.", "real_until_utc": None},
        {"year": 2026, "label": "2026", "real_until_utc": "2026-09-20T05:00:00Z",
         "description": "Real prices through Sep 19. The AI's prediction for the rest of the year isn't ready yet."}]
    by_year = {y: [p for p in s["presets"] if p["year"] == y] for y in (2025, 2026)}
    assert sum(len(v) for v in by_year.values()) == len(s["presets"])
    for year, items in by_year.items():  # each year has a "From January 1" preset, from local midnight
        jan1 = [p for p in items if p["label"] == "From January 1"]
        assert len(jan1) == 1 and jan1[0]["start_utc"] == f"{year}-01-01T06:00:00Z"
        assert jan1[0]["start_local"] == f"{year}-01-01T00:00:00-06:00" and jan1[0]["date_local"] == f"{year}-01-01"
    end = next(p for p in by_year[2026] if p["id"] == "real_prices_end_2026")  # the evening before real prices end
    assert (end["label"], end["start_utc"], end["start_local"], end["date_local"]) == (
        "Where real prices end", "2026-09-19T23:00:00Z", "2026-09-19T18:00:00-05:00", "2026-09-19")
    spike = next(p for p in by_year[2026] if p["id"] == "spike_2026")
    assert (spike["start_utc"], spike["date_local"]) == ("2026-04-24T21:00:00Z", "2026-04-24")
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
    r = client.post("/run/start", json={"year": 2024})
    assert r.status_code == 400 and r.json()["error"] == "invalid_year" and "2025 or 2026" in r.json()["message"]
    r = client.post("/run/start", json={"year": 2026, "preset": "summer_spike"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_preset" and "2025, not 2026" in r.json()["message"]
    r = client.post("/run/speed", json={"speed_tps": 7})
    assert r.status_code == 400 and r.json()["error"] == "invalid_speed"
    r = client.post("/availability", json={"scope": "planet"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope"
    r = client.post("/chaos/kill?worker_id=seven")
    assert r.status_code == 400 and r.json()["error"] == "invalid_worker"


def test_simulator_unreachable_is_a_503(client):
    r = client.post("/run/pause")
    assert r.status_code == 503 and r.json()["error"] == "simulator_unavailable"
    r = client.post("/chaos/kill")  # open to everyone: no passcode, it goes straight to the simulator
    assert r.status_code == 503 and r.json()["error"] == "simulator_unavailable"


def test_presenter_mode_is_gone(client):
    assert client.post("/presenter/check").status_code == 404
    assert not hasattr(main.state.limits, "wrong_passcode")
    import importlib.util

    assert importlib.util.find_spec("src.api.presenter") is None


def test_chaos_kill_is_limited_to_one_per_2s(client):
    first = client.post("/chaos/kill", headers={"X-Presenter-Passcode": "ignored"})  # no header is needed or read
    assert first.status_code == 503  # counted, then the (absent) simulator didn't answer
    second = client.post("/chaos/kill?worker_id=3")
    assert second.status_code == 429 and second.json()["error"] == "rate_limited"
    assert second.json()["retry_after_s"] in (1, 2) and second.headers["Retry-After"] == str(second.json()["retry_after_s"])
    assert client.post("/chaos/kill?worker_id=x").status_code == 400  # malformed ids don't wait or spend the budget
    main.state.limits.chaos_kill.reset()
    assert client.post("/chaos/kill?worker_id=3").status_code == 503


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
                                                   "Access-Control-Request-Headers": "content-type"})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origin
        allowed = {h.strip().lower() for h in r.headers["access-control-allow-headers"].split(",")}
        assert "content-type" in allowed and "x-presenter-passcode" not in allowed
    r = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers["access-control-expose-headers"].lower() == "retry-after"
    for header in ("x-evil-header", "x-presenter-passcode", "x-presenter-passcode,content-type"):
        r = client.options("/chaos/kill", headers={"Origin": "http://localhost:5173",
                                                    "Access-Control-Request-Method": "POST",
                                                    "Access-Control-Request-Headers": header})
        assert r.status_code == 400, header  # only Content-Type is allowed
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
                               ("get", "/results/ai-gains", None),
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

    def ai_gains():
        if results is None:
            raise ResultsNotFound("There are no full-year results yet.")
        return {"ai": "forecast", "baseline": "bands", "years": [{"year": 2025, "is_pitch": True, "strategies": {}}]}

    svc.ResultsNotFound, svc.start_backtest, svc.get_job, svc.pitch_results = ResultsNotFound, start_backtest, get_job, pitch_results
    svc.ai_gains = ai_gains
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
    assert client.get("/results/ai-gains").json()["years"][0]["year"] == 2025
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
    for path in ("/results/pitch", "/results/ai-gains"):
        r = client.get(path)
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


# --- the live map: GET /fleet/layout, and /run/start while the simulator restarts ------------------------


def test_fleet_layout_contract_before_the_simulator_is_up(client):
    """docs/API.md Layout, from config.yaml while Redis is unreachable (cold start): the map can draw at once."""
    import base64
    import json
    import re

    import numpy as np

    from src.config import REPO_ROOT, load_config
    from src.sim import geo
    from src.sim.fleet import location_counts

    cfg = load_config()
    r = client.get("/fleet/layout")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    layout = r.json()
    example = json.loads(re.search(r"\*\*Layout\*\* \(`GET /fleet/layout`\)\s*```json\s*(\{.*?\})\s*```",
                                   (REPO_ROOT / "docs" / "API.md").read_text(), re.S).group(1))
    assert set(layout) == set(example) and all(set(c) == set(example["cities"][0]) for c in layout["cities"])
    assert layout["fleet_size"] == cfg.fleet.size
    counts = location_counts(cfg.fleet.size, cfg.fleet.locations)
    firsts = np.concatenate([[0], np.cumsum(list(counts.values()))[:-1]]).tolist()
    assert [(c["location"], c["batteries"], c["first_battery"]) for c in layout["cities"]] == \
        list(zip(counts, counts.values(), firsts))
    assert [(c["name"], c["short"]) for c in layout["cities"]] == [
        ("Houston", "HOU"), ("Dallas–Fort Worth", "DFW"), ("San Antonio", "SA"), ("Austin", "AUS")]
    raw = base64.b64decode(layout["positions"])
    assert len(raw) == 4 * cfg.fleet.size  # little-endian Int16 [lon, lat] per battery
    v = np.frombuffer(raw, dtype="<i2").reshape(-1, 2)
    lon, lat = -100 + v[:, 0] / 1000, 31 + v[:, 1] / 1000  # the contract's decoding
    assert np.array_equal(v, geo.quantize(geo.fleet_positions(list(counts.items()), cfg.seed)))
    for c in layout["cities"]:
        sl = slice(c["first_battery"], c["first_battery"] + c["batteries"])
        d = geo.distance_km(geo.city(c["location"]), lon[sl], lat[sl])
        assert d.max() <= geo.city(c["location"]).radius_km + 0.2
    assert layout["version"] == geo.layout_version(cfg.fleet.size, cfg.fleet.locations, cfg.seed)
    assert r.headers["etag"] == f'"{layout["version"]}"' and "max-age" in r.headers["cache-control"]
    assert client.get("/fleet/layout", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    assert client.get("/fleet/layout").content == r.content  # cached, byte for byte


def test_start_waits_for_a_restarting_simulator_then_says_so(client, monkeypatch):
    """While a reset respawns the workers (snapshot 'restarting'), /run/start waits (up to ~15 s in production),
    then answers 503 simulator_restarting with retry_after_s and a Retry-After header."""
    import time

    monkeypatch.setenv("FLEET_START_READY_TIMEOUT_S", "0.4")
    backend = main.backend()
    monkeypatch.setattr(backend, "coordinator_snapshot", lambda: {"status": "idle", "run_id": None,
                                                                   "restarting": True, "workers_starting": 20})
    t0 = time.monotonic()
    r = client.post("/run/start", json={"speed_tps": 2})
    assert time.monotonic() - t0 >= 0.4
    body = r.json()
    assert r.status_code == 503 and body["error"] == "simulator_restarting" and body["message"].endswith(".")
    assert body["retry_after_s"] >= 1 and r.headers["Retry-After"] == str(body["retry_after_s"])
    # once the controllers are up, the Start goes through to the coordinator (none answers here: 503 unavailable)
    main.state.limits.run_start.reset()
    flips = iter([None, {"restarting": True}, {"restarting": False, "workers_starting": 0}])
    monkeypatch.setattr(backend, "coordinator_snapshot",
                        lambda: next(flips, {"restarting": False, "workers_starting": 0}))
    r = client.post("/run/start", json={"speed_tps": 2})
    assert r.status_code == 503 and r.json()["error"] == "simulator_unavailable"
    # no coordinator at all (it never published a snapshot): after the wait, simulator_unavailable
    main.state.limits.run_start.reset()
    monkeypatch.setattr(backend, "coordinator_snapshot", lambda: None)
    r = client.post("/run/start", json={"speed_tps": 2})
    assert r.status_code == 503 and r.json()["error"] == "simulator_unavailable" and "retry_after_s" not in r.json()
