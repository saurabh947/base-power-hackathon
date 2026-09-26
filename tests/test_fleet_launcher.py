"""src.launcher supervises the coordinator (which supervises the workers) and the API:
SIGTERM stops everything with exit 0; a dead coordinator stops everything with exit 1, and
orphaned workers notice their parent is gone and exit (macOS has no PDEATHSIG)."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from src.config import REPO_ROOT
from src.sim.redis_layout import Keys, connect, reachable, redis_url

pytestmark = pytest.mark.redis


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if pred():
                return True
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(0.2)
    return False


@pytest.fixture
def stack():
    url = redis_url()
    if not reachable(url):
        pytest.skip(f"Redis not reachable at {url}")
    port = _free_port()
    prefix = f"test:launch:{uuid.uuid4().hex[:8]}:"
    env = {**os.environ, "REDIS_URL": url, "FLEET_REDIS_PREFIX": prefix, "PORT": str(port), "FLEET_SIZE": "200",
           "FLEET_WORKERS": "3", "START_REDIS": "0", "PRESENTER_PASSCODE_FILE": "/nonexistent"}
    for var in ("DATABASE_URL", "DB_HOST"):
        env.pop(var, None)
    proc = subprocess.Popen([sys.executable, "-m", "src.launcher"], cwd=str(REPO_ROOT), env=env,
                            stdin=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    r = connect(url)
    keys = Keys(prefix)
    try:
        assert _wait(lambda: httpx.get(f"{base}/health", timeout=1).json()["ok"], 40), "API never came up"
        assert _wait(lambda: [w["status"] for w in httpx.get(f"{base}/state", timeout=2).json()["workers"]]
                     == ["healthy"] * 3, 40), "workers never became healthy"
        workers = [w["pid"] for w in httpx.get(f"{base}/state", timeout=2).json()["workers"]]
        coordinator = json.loads(r.get(keys.coordinator))["pid"]
        yield proc, base, workers, coordinator
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        for key in r.scan_iter(match=prefix + "*", count=1000):
            r.unlink(key)


def _nice(pid: int) -> int:
    out = subprocess.run(["ps", "-o", "ni=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    return int(out.stdout.strip())


def test_sigterm_stops_everything_cleanly(stack):
    proc, base, workers, coordinator = stack
    api = [int(p) for p in subprocess.run(["pgrep", "-P", str(proc.pid), "-f", "uvicorn"], capture_output=True,
                                          text=True).stdout.split()]
    assert len(api) == 1 and _nice(api[0]) == 5 and _nice(coordinator) == 0  # backtests can't starve the fleet
    assert httpx.post(f"{base}/run/start", json={"speed_tps": 10, "strategy": "peak"}, timeout=10).status_code == 202
    assert _wait(lambda: httpx.get(f"{base}/state", timeout=2).json()["totals"]["ticks"] >= 3, 20)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 0
    assert _wait(lambda: not any(_alive(p) for p in workers + [coordinator]), 10)


def test_a_killed_launcher_leaves_no_orphans(stack):
    proc, base, workers, coordinator = stack
    proc.send_signal(signal.SIGKILL)  # no chance to clean up: children must notice on their own
    proc.wait(timeout=10)
    assert _wait(lambda: not any(_alive(p) for p in workers + [coordinator]), 15)
    assert _wait(lambda: not _responds(base), 15)  # the API followed its parent too


def _responds(base: str) -> bool:
    try:
        httpx.get(f"{base}/health", timeout=1)
        return True
    except httpx.HTTPError:
        return False


def test_a_dead_coordinator_stops_the_stack_with_an_error(stack):
    proc, base, workers, coordinator = stack
    os.kill(coordinator, signal.SIGKILL)
    assert proc.wait(timeout=30) == 1
    assert _wait(lambda: not any(_alive(p) for p in workers), 10)  # orphans noticed their parent is gone
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{base}/health", timeout=1)
