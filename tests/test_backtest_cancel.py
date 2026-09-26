"""POST /backtest/{job_id}/cancel (docs/API.md): stop a running full-year re-run from the Results page.

- A running job: 200 {"status": "cancelled"}, then GET /backtest/{id} says cancelled; its process group gets
  SIGTERM (SIGKILL after a grace period if it ignores that); NOTHING it computed is stored, the previous results
  stay; a backtest_done event says it was stopped. A new start then works.
- A finished job: 409 not_running. An unknown id: 404.
The runner is replaced by small stand-in processes (a real full-year run takes a minute) that write what the real
runner writes; one test cancels the real runner. No database here (tests/test_backtest_store_pg.py has Postgres).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from functools import lru_cache

import pytest
from fastapi.testclient import TestClient

from src.api import main
from src.backtest import service, store


@lru_cache(maxsize=1)
def _mini_records_json() -> str:
    """Records of a mini backtest (bands, peak and the ceiling; 20 batteries, two weeks of real June 2025 prices)."""
    from datetime import date, timedelta

    from src.backtest.engine import local_midnight_utc
    from src.backtest.runner import backtest_settings, run_backtest
    from src.feeds.memory import InMemoryPriceFeed
    from src.feeds.parquet import load_rt_rows

    start = local_midnight_utc(date(2025, 6, 1))
    rows = [r for r in load_rt_rows(years=(2025,)) if start - timedelta(days=7) <= r.ts < start + timedelta(days=14)]
    settings = backtest_settings()
    settings["ceiling"].update(ratio_step=0.25, workers=1)
    outcome = run_backtest(["bands", "peak"], 2025, settings=settings, pitch=True, use_db=False, fleet_size=20,
                           feed=InMemoryPriceFeed.from_intervals(rows), out_dir=tempfile.mkdtemp(),
                           log=lambda s: None, save=False)
    assert not outcome.json_paths  # save=False stored nothing
    return json.dumps(store.clean(outcome.records))


def mini_records() -> list[dict]:
    return json.loads(_mini_records_json())


@pytest.fixture
def results(tmp_path, monkeypatch):
    """A results directory that already holds a pitch set (from a mini backtest on real June 2025 prices)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    out = tmp_path / "results"
    records = mini_records()
    store.save_records(records, pitch=True, use_db=False, out_dir=out)
    monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(out))
    return out, records


@pytest.fixture
def client(monkeypatch, results):
    main.configure(url="redis://127.0.0.1:1/0", prefix="test:cancel:")  # nothing listens: events are captured
    events = []
    seen = set()

    def emit(kind, severity, message, *, sim_ts=None, detail=None, once_key=None):
        if once_key in seen:
            return
        seen.add(once_key)
        events.append({"kind": kind, "severity": severity, "message": message, "detail": detail or {}})

    monkeypatch.setattr(main.backend(), "emit", emit)
    monkeypatch.setattr(service, "CANCEL_GRACE_S", 0.5)
    with TestClient(main.app) as c:
        yield c, events
    for job_id, job in list(service._jobs.items()):  # leave nothing running for the next test
        if job["status"] == "running":
            try:
                service.cancel_job(job_id)
            except service.JobNotRunning:
                pass
        job["settled"].wait(10)
    main.state.backend = None


def snapshot(path) -> dict:
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def stand_in(delay_s: float, records_path=None, *, exit_code: int = 0, ignore_term: bool = False):
    """A stand-in for the runner process: optionally ignores SIGTERM, sleeps, then writes what the real runner
    writes (the records it computed, then the status file) and exits."""

    def command(names, year, status_file, records_file=None):
        lines = ["import json, shutil, signal, sys, time"]
        if ignore_term:
            lines.append("signal.signal(signal.SIGTERM, signal.SIG_IGN)")
        lines.append(f"time.sleep({delay_s})")
        if records_file:
            lines.append(f"shutil.copyfile({str(records_path)!r}, {records_file!r})" if records_path
                         else f"open({records_file!r}, 'w').write('[]')")
        ids = []
        if records_path:
            ids = [r["run_id"] for r in json.loads(open(records_path).read()) if r["strategy"] != "hindsight"]
        status = {"run_ids": ids, "skipped": {}, "error": None}
        lines.append(f"open({status_file!r}, 'w').write({json.dumps(status)!r})")
        lines.append(f"sys.exit({exit_code})")
        return [sys.executable, "-c", "\n".join(lines)]

    return command


def fresh_records(tmp_path, records):
    """The same records under new run ids: what a new run would store (SYNTHETIC ids, real numbers)."""
    import uuid

    path = tmp_path / "new_records.json"
    path.write_text(json.dumps([dict(r, run_id=str(uuid.uuid4())) for r in records]))
    return path


def started(job_id, timeout=10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = service._jobs[job_id].get("pid")
        if pid:
            return pid
        time.sleep(0.02)
    raise AssertionError("the job's process never started")


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_cancel_a_running_job(client, results, tmp_path, monkeypatch):
    c, events = client
    out, records = results
    new = fresh_records(tmp_path, records)
    monkeypatch.setattr(service, "runner_command", stand_in(30.0, new))  # it would store a new pitch set at 30 s
    before, pitch = snapshot(out), c.get("/results/pitch").json()
    job_id = c.post("/backtest", json={"strategies": ["bands", "peak"], "year": 2025}).json()["job_id"]
    pid = started(job_id)
    assert c.get(f"/backtest/{job_id}").json()["status"] == "running"
    t0 = time.monotonic()
    r = c.post(f"/backtest/{job_id}/cancel")
    assert r.status_code == 200 and r.json() == {"status": "cancelled"}
    assert c.get(f"/backtest/{job_id}").json() == {"status": "cancelled", "run_ids": [], "skipped": {}, "error": None}
    assert service._jobs[job_id]["settled"].wait(5) and gone(pid)
    assert time.monotonic() - t0 < 2.0  # stopped promptly, not after the 30 s the run would have taken
    time.sleep(0.3)
    assert snapshot(out) == before and c.get("/results/pitch").json() == pitch  # nothing stored
    assert c.get(f"/backtest/{job_id}").json()["status"] == "cancelled"  # the job thread kept it cancelled
    (event,) = [e for e in events if e["kind"] == "backtest_done"]
    assert event["message"] == "The full-year re-run was stopped. The previous results are still shown."
    assert event["detail"]["job_id"] == job_id and event["detail"]["status"] == "cancelled"
    assert event["severity"] == "info"


def test_a_job_that_ignores_sigterm_is_killed_after_the_grace_period(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(service, "runner_command", stand_in(30.0, ignore_term=True))
    job_id = c.post("/backtest", json={"strategies": ["bands"], "year": 2025}).json()["job_id"]
    pid = started(job_id)
    time.sleep(0.3)  # let it install its SIGTERM handler
    t0 = time.monotonic()
    assert c.post(f"/backtest/{job_id}/cancel").json() == {"status": "cancelled"}
    assert service._jobs[job_id]["settled"].wait(5) and gone(pid)
    assert 0.4 <= time.monotonic() - t0 < 3.0  # SIGTERM was ignored; SIGKILL after the 0.5 s grace period


def test_cancel_after_done_is_409_and_unknown_is_404(client, results, tmp_path, monkeypatch):
    c, events = client
    out, records = results
    new = fresh_records(tmp_path, records)
    monkeypatch.setattr(service, "runner_command", stand_in(0.0, new))
    job_id = c.post("/backtest", json={"strategies": ["bands", "peak"], "year": 2025}).json()["job_id"]
    assert service._jobs[job_id]["settled"].wait(10)
    done = c.get(f"/backtest/{job_id}").json()
    assert done["status"] == "done" and len(done["run_ids"]) == 2
    # a finished job's records are stored by the job itself (the runner stored nothing): the new pitch set
    pitch = {r["run_id"] for r in store.load_pitch_records_json(2025, out)}
    assert pitch == {r["run_id"] for r in json.loads(new.read_text())}
    r = c.post(f"/backtest/{job_id}/cancel")
    assert r.status_code == 409 and r.json() == {"error": "not_running",
                                                 "message": "That full-year run has already finished."}
    assert c.get(f"/backtest/{job_id}").json()["status"] == "done"
    r = c.post("/backtest/no-such-job/cancel")
    assert r.status_code == 404 and r.json()["error"] == "unknown_job"
    assert not [e for e in events if "stopped" in e["message"]]


def test_start_again_after_a_cancel(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(service, "runner_command", stand_in(30.0))
    first = c.post("/backtest", json={"strategies": ["bands"], "year": 2025}).json()["job_id"]
    started(first)
    assert c.post(f"/backtest/{first}/cancel").json() == {"status": "cancelled"}
    r = c.post(f"/backtest/{first}/cancel")  # a second cancel: it has finished (cancelled)
    assert r.status_code == 409 and r.json()["error"] == "not_running"
    monkeypatch.setattr(service, "runner_command", stand_in(0.2))
    r = c.post("/backtest", json={"strategies": ["peak"], "year": 2025})
    second = r.json()["job_id"]
    assert r.status_code == 202 and second != first
    assert service._jobs[second]["settled"].wait(10)
    assert c.get(f"/backtest/{second}").json()["status"] == "done"
    assert c.get(f"/backtest/{first}").json()["status"] == "cancelled"


def test_a_cancel_that_lands_while_the_results_are_being_stored_waits_then_is_409(client, results, tmp_path,
                                                                                    monkeypatch):
    """Past the point of no return (the run finished and its records are being stored), a cancel lets the store
    land and answers 409: the job is done, and its results are whole."""
    c, _ = client
    out, records = results
    new = fresh_records(tmp_path, records)
    monkeypatch.setattr(service, "runner_command", stand_in(0.0, new))
    real_save = store.save_records
    storing = []

    def slow_save(*args, **kwargs):
        storing.append(time.monotonic())
        time.sleep(1.0)
        return real_save(*args, **kwargs)

    monkeypatch.setattr(store, "save_records", slow_save)
    job_id = c.post("/backtest", json={"strategies": ["bands", "peak"], "year": 2025}).json()["job_id"]
    deadline = time.monotonic() + 10
    while not storing and time.monotonic() < deadline:
        time.sleep(0.02)
    assert storing
    r = c.post(f"/backtest/{job_id}/cancel")
    assert r.status_code == 409 and c.get(f"/backtest/{job_id}").json()["status"] == "done"
    assert {r["run_id"] for r in store.load_pitch_records_json(2025, out)} == {
        r["run_id"] for r in json.loads(new.read_text())}


def test_cancel_the_real_runner(client, results):
    """The real `python -m src.backtest.runner` (a full year at 20,000 batteries): stopped at once, nothing stored."""
    c, _ = client
    out, _ = results
    before = snapshot(out)
    job_id = c.post("/backtest", json={"strategies": ["peak"], "year": 2025}).json()["job_id"]
    pid = started(job_id)
    time.sleep(1.5)  # loading prices and building the fleet
    assert c.post(f"/backtest/{job_id}/cancel").json() == {"status": "cancelled"}
    assert service._jobs[job_id]["settled"].wait(5) and gone(pid)
    assert snapshot(out) == before
    assert c.get(f"/backtest/{job_id}").json() == {"status": "cancelled", "run_ids": [], "skipped": {}, "error": None}


def test_the_bridge_answers_501_until_the_service_lands(client, monkeypatch):
    c, _ = client
    real_import = main.importlib.import_module

    def missing(name, *a, **kw):
        if name == "src.backtest.service":
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(main.importlib, "import_module", missing)
    r = c.post("/backtest/abc/cancel")
    assert r.status_code == 501 and r.json()["error"] == "not_ready"


def test_the_message_when_there_were_no_results_before(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(tmp_path / "empty"))
    assert service.stopped_message() == ("The full-year run was stopped before it finished, so there are no "
                                         "full-year results yet.")
    shutil.rmtree(tmp_path / "empty", ignore_errors=True)
