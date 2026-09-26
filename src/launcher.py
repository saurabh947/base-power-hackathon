"""One-container entry point (Cloud Run): Redis, migrations, the coordinator and the API.

Run:  python -m src.launcher

1. START_REDIS=1: start redis-server on REDIS_URL's port (save '' and appendonly no:
   sim state is meant to be ephemeral) and wait for PING.
2. If a database is configured (DATABASE_URL or DB_HOST): src.db.migrate().
3. Start the coordinator process (it spawns and supervises the 20 workers) and uvicorn
   serving src.api.main:app on $PORT (default 8000).
4. Supervise: if Redis, the coordinator or the API dies, stop everything and exit
   non-zero. SIGTERM/SIGINT stop everything gracefully (exit 0).
Children learn our pid via FLEET_PARENT_PID and exit if we die. Zombies: every child is
polled (reaped); in the container tini (PID 1) reaps any orphaned grandchildren.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from urllib.parse import urlparse

from src.config import REPO_ROOT


def _log(msg: str) -> None:
    print(f"[launcher {os.getpid()}] {msg}", file=sys.stderr, flush=True)


def _wait_redis(url: str, timeout_s: float, proc: subprocess.Popen | None) -> bool:
    import redis

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            if redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1).ping():
                return True
        except redis.RedisError:
            pass
        time.sleep(0.1)
    return False


def _start_redis(url: str) -> subprocess.Popen:
    u = urlparse(url)
    port = str(u.port or 6379)
    host = u.hostname or "127.0.0.1"
    cmd = [os.environ.get("REDIS_SERVER", "redis-server"), "--port", port, "--bind", host, "--save", "",
           "--appendonly", "no", "--protected-mode", "no", "--loglevel", "warning"]
    _log(f"starting redis-server on {host}:{port}")
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL)


def _migrate() -> None:
    if not (os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST")):
        _log("no database configured; skipping migrations")
        return
    try:
        from src.db import migrate

        applied = migrate()
        _log(f"migrations applied: {', '.join(applied) or 'nothing new'}")
    except Exception as exc:  # the live fleet runs without a database; backtests/forecasts report not_ready
        _log(f"migrations failed: {exc!r}")


def _terminate(procs: dict[str, subprocess.Popen], order: list[str], timeout_s: float = 10.0) -> None:
    for name in order:
        p = procs.get(name)
        if p is None or p.poll() is not None:
            continue
        _log(f"stopping {name} (pid {p.pid})")
        try:
            p.terminate()
        except ProcessLookupError:
            continue
        try:
            p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _log(f"{name} did not stop in {timeout_s:g}s; killing it")
            p.kill()
            p.wait(timeout=5)


def main() -> int:
    url = os.environ.get("REDIS_URL") or "redis://127.0.0.1:6379/0"
    os.environ["REDIS_URL"] = url
    port = os.environ.get("PORT") or "8000"
    stopping = {"flag": False}

    def _on_signal(signum, _frame):
        _log(f"signal {signum}: shutting down")
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    procs: dict[str, subprocess.Popen] = {}
    if os.environ.get("START_REDIS") == "1":
        procs["redis"] = _start_redis(url)
    if not _wait_redis(url, 15.0, procs.get("redis")):
        _log(f"redis at {url} is not reachable")
        _terminate(procs, ["redis"])
        return 1
    _migrate()

    env = {**os.environ, "FLEET_PARENT_PID": str(os.getpid()), "PYTHONUNBUFFERED": "1"}
    procs["coordinator"] = subprocess.Popen([sys.executable, "-m", "src.sim.coordinator", "serve",
                                             "--parent-pid", str(os.getpid())],
                                            cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL)
    procs["api"] = subprocess.Popen([sys.executable, "-m", "uvicorn", "src.api.main:app", "--host", "0.0.0.0",
                                     "--port", port, "--proxy-headers", "--forwarded-allow-ips", "*",
                                     "--no-access-log", "--timeout-graceful-shutdown", "5"],
                                    cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL)
    _log(f"coordinator pid {procs['coordinator'].pid}, api pid {procs['api'].pid} on :{port}")

    code = 0
    while not stopping["flag"]:
        for name, p in procs.items():
            rc = p.poll()
            if rc is not None:
                _log(f"{name} exited with {rc}; stopping everything")
                code = 1
                stopping["flag"] = True
                break
        else:
            time.sleep(0.5)
    _terminate(procs, ["api", "coordinator"])
    _terminate(procs, ["redis"], timeout_s=5.0)
    return code


if __name__ == "__main__":
    sys.exit(main())
