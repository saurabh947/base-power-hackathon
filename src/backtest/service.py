"""The API bridge for full-year results (docs/API.md: POST /backtest, GET /backtest/{job_id},
POST /backtest/{job_id}/cancel, GET /results/pitch, GET /results/ai-gains). The API calls exactly
these functions:

    start_backtest(strategies, year) -> job_id
    get_job(job_id)                  -> {"status": "running" | "done" | "failed" | "cancelled", "run_ids": [...], "skipped": {...}, "error": None}
    cancel_job(job_id)               -> {"status": "cancelled"}
    pitch_results(year=None)         -> the Results object of that year's pitch set (default: the pitch year)
    ai_gains(year=None)              -> the AiGains object (what the AI forecast adds; that year first)
    stopped_message(job_id=None)     -> the backtest_done sentence for a cancelled job

Every year has its own pitch set (is_pitch is per year): a job for 2026 replaces 2026's set only, never 2025's.

A job runs `python -m src.backtest.runner --pitch --records-out FILE` in a subprocess (its own
process group), so the replay's CPU work never holds the API process's GIL (the live coordinator
runs there). The subprocess stores nothing: it writes the finished records to FILE, and this
process stores them (backtest_runs and data/results/, as the pitch set) only if the job wasn't
cancelled first. That decision is taken under the job lock, so a cancelled job never leaves a
trace in the results, whenever the cancel lands. One job at a time: while one runs,
start_backtest returns its job_id instead of starting another. Job state lives in this process's
memory (Cloud Run runs one instance).

Cancelling: the job is marked cancelled at once, its process group gets SIGTERM and, if still
alive after CANCEL_GRACE_S, SIGKILL. A job already storing its finished records can't be
cancelled any more: cancel_job waits for it to land and raises JobNotRunning, as for any job
that has finished. A new start after a cancel works at once.

Errors: start_backtest raises ValueError (a plain-language message) for unknown strategies or
a bad year; get_job and cancel_job raise KeyError for an unknown job; cancel_job raises
JobNotRunning for a job that has finished; pitch_results and ai_gains raise ResultsNotFound
when no full-year results exist yet. A failed job's `error` is one plain-language sentence.
"""
from __future__ import annotations

import json
import logging
import os
import signal
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
CANCEL_GRACE_S = 3.0  # SIGTERM first; SIGKILL if the job's processes are still alive after this
SAVE_WAIT_S = 60.0  # how long a cancel waits for a job that is already storing its finished records


class ResultsNotFound(LookupError):
    """No full-year results for that year yet (the API answers 404)."""


class JobNotRunning(RuntimeError):
    """cancel_job on a job that has already finished (the API answers 409 not_running)."""


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
                         "skipped": {}, "started_utc": _now(), "proc": None, "pid": None, "saving": False,
                         "settled": threading.Event()}
        _current = job_id
        _trim()
    threading.Thread(target=_run_job, args=(job_id, names, int(year)), name=f"backtest-{job_id[:8]}",
                     daemon=True).start()
    return job_id


def get_job(job_id: str) -> dict:
    """{"status", "run_ids", "skipped", "error"} for a job started by this process. KeyError if unknown."""
    with _lock:
        job = _jobs[job_id]
        return {"status": job["status"], "run_ids": list(job["run_ids"]),
                "skipped": dict(job.get("skipped") or {}), "error": job["error"]}


def cancel_job(job_id: str) -> dict:
    """Stop a running job: {"status": "cancelled"}. Nothing it computed is stored.

    KeyError for an unknown job; JobNotRunning once it has finished (done, failed or cancelled), including a
    job that was already storing its finished records when the cancel came (it is left to land first).
    """
    with _lock:
        job = _jobs[job_id]
        cancellable = job["status"] == "running" and not job["saving"]
        if cancellable:
            job.update(status="cancelled", run_ids=[], skipped={}, error=None, finished_utc=_now())
        proc, storing, settled = job["proc"], job["saving"], job["settled"]
    if not cancellable:
        if storing:  # it finished computing and is storing its records: let that land, then it has finished
            settled.wait(SAVE_WAIT_S)
        raise JobNotRunning("That full-year run has already finished.")
    if proc is not None:  # else the job thread stops it the moment it has started it
        _stop(proc)
    return {"status": "cancelled"}


def stopped_message(job_id: str | None = None) -> str:
    """The backtest_done sentence for a cancelled job (docs/UX.md tone), about that job's year."""
    with _lock:
        year = (_jobs.get(job_id) or {}).get("year") if job_id else None
    try:
        pitch_results(year)
    except ResultsNotFound:
        if year is not None and year != _pitch_year():
            return (f"The full-year {year} run was stopped before it finished, so there are no full-year results "
                    f"for {year} yet.")
        return "The full-year run was stopped before it finished, so there are no full-year results yet."
    except Exception:
        pass
    return "The full-year re-run was stopped. The previous results are still shown."


def _pitch_year() -> int:
    from src.backtest.runner import backtest_settings

    return int(backtest_settings()["year"])


def pitch_results(year: int | None = None) -> dict:
    """The Results object of a year's pitch set (default: the pitch year, config backtest.year).

    Reads the is_pitch rows of backtest_runs for that year when a database is configured, else (or if the
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


def ai_gains(year: int | None = None) -> dict:
    """docs/API.md AiGains: what the AI forecast adds over the simple rule, per year, from stored runs.

    The requested year (default: the pitch year, config backtest.year) comes first, as its own is_pitch set; every
    other year present follows with its pitch set if complete, else its latest complete set of bands, forecast and
    peak, on the same fleet (src.backtest.results.build_ai_gains has the rules, including what is left out for runs
    stored before metrics.insights existed). Money is the year's total; a year whose real prices end early (2026)
    also splits it into real and projected. Reads backtest_runs when a database is configured, else (or if it
    fails or holds none) the JSON export in data/results/. Raises ResultsNotFound when no year qualifies.
    """
    from src.backtest import store
    from src.backtest.results import build_ai_gains, config_fleet_signature
    from src.backtest.runner import backtest_settings

    settings = backtest_settings()
    records: list[dict] = []
    if store.db_configured():
        try:
            records = store.load_gains_records()
        except Exception as exc:  # fall back to the JSON export rather than fail the screen
            log.warning("ai_gains: database read failed (%s); using the JSON export", exc)
            records = []
    if not records:
        records = store.load_gains_records_json()
    gains = build_ai_gains(records, pitch_year=int(settings["year"]), year=year, settings=settings,
                           default_signature=config_fleet_signature())
    if not gains["years"]:
        raise ResultsNotFound("There are no full-year results yet.")
    return gains


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


def runner_command(names: list[str], year: int, status_file: str, records_file: str | None = None) -> list[str]:
    cmd = [sys.executable, "-m", "src.backtest.runner", "--strategies", ",".join(names), "--year", str(year),
           "--pitch", "--status-file", status_file]
    return cmd + ["--records-out", records_file] if records_file else cmd


def _signal(proc: subprocess.Popen, sig: int) -> None:
    """Signal the job's process group (the runner and the ceiling's LP pool); the runner alone as a fallback."""
    try:
        os.killpg(proc.pid, sig)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass


def _stop(proc: subprocess.Popen, grace_s: float | None = None) -> None:
    """SIGTERM to the job's process group now; SIGKILL after the grace period if the runner is still alive.
    (The runner has no SIGTERM handler, so it and its LP pool normally die at once.)"""
    _signal(proc, signal.SIGTERM)

    def kill_if_alive() -> None:
        if proc.poll() is None:  # never signal a reaped pid
            _signal(proc, signal.SIGKILL)

    timer = threading.Timer(CANCEL_GRACE_S if grace_s is None else grace_s, kill_if_alive)
    timer.daemon = True
    timer.start()


def _read_json(path: str, default):
    text = Path(path).read_text() if Path(path).exists() else ""
    return json.loads(text) if text.strip() else default


def _run_job(job_id: str, names: list[str], year: int) -> None:
    status: dict = {}
    records: list = []
    error = None
    files = []
    for suffix in (".json", "-records.json"):
        fd, path = tempfile.mkstemp(prefix=f"backtest-{job_id[:8]}-", suffix=suffix)
        os.close(fd)
        files.append(path)
    status_file, records_file = files
    try:
        # its own process group, so a cancel stops the runner and the ceiling's LP pool together
        proc = subprocess.Popen(runner_command(names, year, status_file, records_file), cwd=REPO_ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy(),
                                start_new_session=True)
        with _lock:
            job = _jobs[job_id]
            job.update(proc=proc, pid=proc.pid)
            cancelled = job["status"] == "cancelled"
        if cancelled:  # the cancel came before the process existed
            _stop(proc)
        try:
            _, stderr = proc.communicate(timeout=JOB_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _stop(proc, grace_s=0.0)
            _, stderr = proc.communicate()
            error = f"The full-year run took longer than {JOB_TIMEOUT_S // 60} minutes and was stopped."
        status = _read_json(status_file, {})
        if error is None and proc.returncode != 0:
            error = status.get("error") or "The full-year run failed."
            with _lock:
                cancelled = _jobs[job_id]["status"] == "cancelled"
            if not cancelled:
                log.error("backtest job %s failed (exit %s): %s", job_id, proc.returncode, (stderr or "")[-4000:])
        elif error is None:
            records = _read_json(records_file, [])
    except Exception as exc:
        error = f"The full-year run could not start ({type(exc).__name__})."
        log.exception("backtest job %s", job_id)
    finally:
        for path in files:
            Path(path).unlink(missing_ok=True)
            Path(path + ".tmp").unlink(missing_ok=True)
    # store the finished records as the pitch set, unless the job was cancelled first (decided under the lock)
    with _lock:
        job = _jobs[job_id]
        store_them = job["status"] == "running" and error is None
        job["saving"] = store_them
    if store_them and records:
        try:
            from src.backtest import store
            from src.backtest.runner import backtest_settings

            store.save_records(records, pitch=True, settings=backtest_settings())
        except Exception as exc:
            error = "The full-year results couldn't be saved."
            log.exception("backtest job %s: saving failed (%s)", job_id, exc)
    run_ids = list(status.get("run_ids") or [])
    if status.get("ceiling_run_id"):
        run_ids.append(status["ceiling_run_id"])
    with _lock:
        job = _jobs[job_id]
        if job["status"] == "running":
            job.update(status="failed" if error else "done", run_ids=[] if error else run_ids, error=error,
                       skipped=status.get("skipped") or {}, finished_utc=_now())
        job.update(saving=False, proc=None)
        job["settled"].set()
        snapshot = {"status": job["status"], "run_ids": list(job["run_ids"]),
                    "skipped": dict(job.get("skipped") or {}), "error": job["error"]}
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
