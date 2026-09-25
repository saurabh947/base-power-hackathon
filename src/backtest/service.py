"""The API bridge for full-year results (docs/API.md: POST /backtest, GET /backtest/{job_id},
GET /results/pitch). The API builder calls exactly these three functions:

    start_backtest(strategies, year) -> job_id
    get_job(job_id)                  -> {"status": "running" | "done" | "failed", "run_ids": [...], "error": None}
    pitch_results(year=None)         -> the Results object

A job runs `python -m src.backtest.runner --pitch` in a subprocess, so the replay's CPU work
never holds the API process's GIL (the live coordinator runs there). One job at a time: while
one runs, start_backtest returns its job_id instead of starting another. Job state lives in
this process's memory (Cloud Run runs one instance); results live in backtest_runs and
data/results/. A finished job's runs become the Results screen's set (is_pitch).

Errors: start_backtest raises ValueError (a plain-language message) for unknown strategies or
a bad year; get_job raises KeyError for an unknown job; pitch_results raises ResultsNotFound
when no full-year results exist yet. A failed job's `error` is one plain-language sentence.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.config import REPO_ROOT

log = logging.getLogger(__name__)

KNOWN_STRATEGIES = ("bands", "forecast", "peak")
JOB_TIMEOUT_S = 30 * 60
MAX_JOBS_KEPT = 50


class ResultsNotFound(LookupError):
    """No full-year results for that year yet (the API answers 404)."""


_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_current: str | None = None
_listeners: list[Callable[[str, dict], None]] = []


def start_backtest(strategies: list[str], year: int) -> str:
    """Start a full-year backtest job and return its id (or the id of the one already running)."""
    global _current
    names = _validate(strategies, year)
    with _lock:
        if _current is not None and _jobs[_current]["status"] == "running":
            return _current
        job_id = str(uuid.uuid4())
        _jobs[job_id] = {"status": "running", "run_ids": [], "error": None, "strategies": names, "year": int(year),
                         "skipped": {}, "started_utc": _now()}
        _current = job_id
        _trim()
    threading.Thread(target=_run_job, args=(job_id, names, int(year)), name=f"backtest-{job_id[:8]}",
                     daemon=True).start()
    return job_id


def get_job(job_id: str) -> dict:
    """{"status", "run_ids", "error"} for a job started by this process. KeyError if unknown."""
    with _lock:
        job = _jobs[job_id]
        return {"status": job["status"], "run_ids": list(job["run_ids"]), "error": job["error"]}


def pitch_results(year: int | None = None) -> dict:
    """The Results object for the pitch year (config backtest.year by default).

    Reads the is_pitch rows of backtest_runs when a database is configured, else (or if the
    database has none, or cannot be reached) data/results/pitch_<year>.json.
    """
    from src.backtest import store
    from src.backtest.results import build_results
    from src.backtest.runner import backtest_settings

    settings = backtest_settings()
    year = int(year if year is not None else settings["year"])
    if store.db_configured():
        try:
            records = store.load_pitch_records(year)
        except Exception as exc:  # fall back to the JSON export rather than fail the screen
            log.warning("pitch_results: database read failed (%s); using the JSON export", exc)
            records = []
        if records:
            return build_results(records, year=year, settings=settings)
    results = store.read_pitch_results_json(year)
    if results is not None:
        return results
    raise ResultsNotFound(f"There are no full-year results for {year} yet.")


def add_listener(fn: Callable[[str, dict], None]) -> None:
    """Call fn(job_id, get_job(job_id)) when a job finishes (e.g. to emit a backtest_done event)."""
    _listeners.append(fn)


# --- internals -------------------------------------------------------------------------------


def _validate(strategies, year) -> list[str]:
    if isinstance(strategies, str) or not strategies:
        raise ValueError("Pick at least one decision method: bands, forecast or peak.")
    names = []
    for s in strategies:
        if s not in KNOWN_STRATEGIES:
            raise ValueError(f"Unknown decision method {s!r}. Choose from: {', '.join(KNOWN_STRATEGIES)}.")
        if s not in names:
            names.append(s)
    if isinstance(year, bool) or not isinstance(year, int) or not (2000 <= year <= datetime.now().year):
        raise ValueError(f"Pick a past year to replay, not {year!r}.")
    return names


def runner_command(names: list[str], year: int, status_file: str) -> list[str]:
    return [sys.executable, "-m", "src.backtest.runner", "--strategies", ",".join(names), "--year", str(year),
            "--pitch", "--status-file", status_file]


def _run_job(job_id: str, names: list[str], year: int) -> None:
    status: dict = {}
    error = None
    fd, status_file = tempfile.mkstemp(prefix=f"backtest-{job_id[:8]}-", suffix=".json")
    os.close(fd)
    try:
        proc = subprocess.run(runner_command(names, year, status_file), cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=JOB_TIMEOUT_S, env=os.environ.copy())
        text = Path(status_file).read_text()
        status = json.loads(text) if text.strip() else {}
        if proc.returncode != 0:
            error = status.get("error") or "The full-year run failed."
            log.error("backtest job %s failed (exit %s): %s", job_id, proc.returncode, proc.stderr[-4000:])
    except subprocess.TimeoutExpired:
        error = f"The full-year run took longer than {JOB_TIMEOUT_S // 60} minutes and was stopped."
    except Exception as exc:
        error = f"The full-year run could not start ({type(exc).__name__})."
        log.exception("backtest job %s", job_id)
    finally:
        Path(status_file).unlink(missing_ok=True)
    run_ids = list(status.get("run_ids") or [])
    if status.get("ceiling_run_id"):
        run_ids.append(status["ceiling_run_id"])
    with _lock:
        job = _jobs[job_id]
        job.update(status="failed" if error else "done", run_ids=run_ids, error=error,
                   skipped=status.get("skipped") or {}, finished_utc=_now())
        snapshot = {"status": job["status"], "run_ids": list(run_ids), "error": error}
    for fn in list(_listeners):
        try:
            fn(job_id, snapshot)
        except Exception:
            log.exception("backtest listener failed")


def _trim() -> None:
    finished = [j for j, v in _jobs.items() if v["status"] != "running"]
    for j in finished[: max(0, len(_jobs) - MAX_JOBS_KEPT)]:
        del _jobs[j]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
