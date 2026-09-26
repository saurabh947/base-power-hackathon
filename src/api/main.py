"""fleet-sim HTTP + WebSocket API. Implements docs/API.md.

Run:  uvicorn src.api.main:app --port 8000      (src.launcher does this on $PORT)

The API never touches batteries itself: it reads the coordinator's snapshot and streams
from Redis and sends control requests (start, pause, availability, ...) over the control
stream, waiting for the coordinator's reply. The one direct action is POST /chaos/kill,
which SIGKILLs a worker process found in the worker registry (presenter only).

Env: REDIS_URL, FLEET_REDIS_PREFIX, CORS_ORIGINS (comma-separated), PRESENTER_PASSCODE_FILE /
PRESENTER_PASSCODE, FLEET_FRAME_INTERVAL_S (tests), FLEET_CONTROL_TIMEOUT_S,
FLEET_ALLOW_MAX_SPEED=1 (tests/bench only), FLEET_PARENT_PID (set by the launcher).
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api import presenter
from src.api.backend import Backend, kill_worker
from src.api.limits import Limits, retry_after
from src.api.live import LiveCursor, forecasts, frame_message, snapshot_message
from src.config import REPO_ROOT, load_config, load_raw_config
from src.sim import control as ctl

UTC = timezone.utc
CFG = load_config()
RAW = load_raw_config()
API_CFG = ctl.api_section(RAW)


def _version() -> str:
    for var in ("APP_VERSION", "K_REVISION"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        import tomllib

        return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    except Exception:
        return "dev"


VERSION = _version()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class _State:
    backend: Backend | None = None
    limits = Limits(API_CFG.get("rate_limits"))


state = _State()


def configure(*, url: str | None = None, prefix: str | None = None) -> Backend:
    """(Re)point the API at a Redis URL / key prefix (tests)."""
    state.backend = Backend(CFG, RAW, url=url, prefix=prefix)
    return state.backend


def backend() -> Backend:
    if state.backend is None:
        configure()
    return state.backend


def frame_interval_s() -> float:
    return _env_float("FLEET_FRAME_INTERVAL_S", float(API_CFG.get("frame_interval_s", 1.0)))


def control_timeout_s() -> float:
    return _env_float("FLEET_CONTROL_TIMEOUT_S", float(API_CFG.get("control_timeout_s", 4.0)))


def allow_max_speed() -> bool:
    return os.environ.get("FLEET_ALLOW_MAX_SPEED") == "1"


# --- app -----------------------------------------------------------------------------------------


async def _watch_parent() -> None:
    """Exit if the launcher dies (uvicorn would otherwise live on as an orphan)."""
    parent = int(os.environ["FLEET_PARENT_PID"])
    while True:
        await asyncio.sleep(1.0)
        if os.getppid() != parent:
            print("[api] launcher is gone; exiting", file=sys.stderr, flush=True)
            os._exit(0)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    task = asyncio.create_task(_watch_parent()) if os.environ.get("FLEET_PARENT_PID") else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="fleet-sim API", version=VERSION, lifespan=_lifespan)
_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(set(_origins + ["http://localhost:5173"])),
    allow_origin_regex=r"https://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.vercel\.app",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    max_age=600,
)


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    headers = {"Retry-After": str(extra["retry_after_s"])} if "retry_after_s" in extra else None
    return JSONResponse(status_code=status, content={"error": code, "message": message, **extra}, headers=headers)


@app.exception_handler(ctl.ControlError)
async def _control_error(_request: Request, exc: ctl.ControlError):
    return _error(exc.status, exc.code, exc.message, **exc.extra)


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException):
    codes = {404: ("not_found", "There's nothing here."), 405: ("method_not_allowed", "That method isn't allowed here.")}
    code, message = codes.get(exc.status_code, ("http_error", str(exc.detail)))
    return _error(exc.status_code, code, message)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(x) for x in first.get("loc", ()) if x not in ("query", "path", "body"))
    return _error(400, "invalid_request", f"The request isn't valid{f' ({where})' if where else ''}.")


def _limited(wait_s: float) -> ctl.ControlError:
    n = retry_after(wait_s)
    return ctl.ControlError(429, "rate_limited", f"Too many requests. Please try again in {n} second{'s' if n != 1 else ''}.",
                            retry_after_s=n)


async def _body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise ctl.ControlError(400, "invalid_json", "The request body isn't valid JSON.") from None
    if not isinstance(data, dict):
        raise ctl.ControlError(400, "invalid_json", "The request body must be a JSON object.")
    return data


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _call(fn, *args, **kwargs):
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except ctl.ControlError:
        raise
    except Exception as exc:
        import redis

        if isinstance(exc, redis.RedisError):
            raise ctl.ControlError(503, "simulator_unavailable",
                                   "The simulator isn't reachable right now. Please try again in a few seconds.") from exc
        raise


def _control(cmd: str, args: dict | None = None) -> dict:
    return backend().control.request(cmd, args, timeout_s=control_timeout_s())


# --- REST ------------------------------------------------------------------------------------------


@app.get("/health")
async def health():
    status = await run_in_threadpool(lambda: backend().status())
    return {"ok": True, "version": VERSION, "status": status}


@app.get("/settings")
async def settings():
    forecast_available = importlib.util.find_spec("src.strategy.forecast") is not None
    return {
        "defaults": ctl.default_settings(CFG, RAW),
        "editable": list(ctl.EDITABLE),
        "presets": ctl.presets(RAW),
        **ctl.choices(RAW, forecast_available),
    }


@app.get("/state")
async def get_state():
    return await _call(snapshot_message, backend())


@app.post("/run/start", status_code=202)
async def run_start(request: Request):
    body = await _body(request)
    editable = {k: body[k] for k in ctl.EDITABLE if k in body}
    ctl.resolve_settings(editable, CFG, RAW, allow_max=allow_max_speed())  # 400 before spending the rate limit
    wait = state.limits.run_start.hit()
    if wait is not None:
        raise _limited(wait)
    result = await _call(_control, "start", editable)
    return JSONResponse(status_code=202, content=result)


@app.post("/run/pause")
async def run_pause():
    return await _call(_control, "pause")


@app.post("/run/resume")
async def run_resume():
    return await _call(_control, "resume")


@app.post("/run/speed")
async def run_speed(request: Request):
    body = await _body(request)
    speed = ctl.parse_speed(body.get("speed_tps"), allow_max=allow_max_speed())
    return await _call(_control, "speed", {"speed_tps": speed})


@app.post("/run/reset")
async def run_reset():
    wait = state.limits.run_reset.hit()
    if wait is not None:
        raise _limited(wait)
    return await _call(_control, "reset")


@app.post("/availability")
async def availability(request: Request):
    body = await _body(request)
    if body.get("scope") not in ("location", "worker", "battery", "all"):
        raise ctl.ControlError(400, "invalid_scope", "scope must be one of: location, worker, battery, all.")
    wait = state.limits.availability.hit()
    if wait is not None:
        raise _limited(wait)
    return await _call(_control, "availability", body)


@app.get("/batteries/{battery_id}")
async def get_battery(battery_id: str):
    return await _call(lambda: backend().battery(battery_id))


def _require_presenter(request: Request) -> None:
    expected = presenter.configured_passcode()
    if expected is None:
        raise ctl.ControlError(503, "presenter_passcode_not_set", "Presenter mode isn't set up on this server yet.")
    ip = _client_ip(request)
    wait = state.limits.wrong_passcode.blocked(ip)
    if wait is not None:
        raise _limited(wait)
    if not presenter.matches(request.headers.get("x-presenter-passcode"), expected):
        state.limits.wrong_passcode.record(ip)
        raise ctl.ControlError(403, "forbidden", "That presenter passcode isn't right.")


@app.post("/presenter/check")
async def presenter_check(request: Request):
    _require_presenter(request)
    return {"ok": True}


@app.post("/chaos/kill")
async def chaos_kill(request: Request):
    _require_presenter(request)
    raw = request.query_params.get("worker_id")
    worker_id = None
    if raw not in (None, ""):
        try:
            worker_id = int(raw)
        except ValueError:
            raise ctl.ControlError(400, "invalid_worker", "worker_id must be a whole number.") from None
    return await _call(kill_worker, backend(), worker_id)


# --- bridges to modules built in parallel --------------------------------------------------------


def _bridge(module: str):
    try:
        return importlib.import_module(module)
    except ImportError:
        raise ctl.ControlError(501, "not_ready", "This part of the simulator isn't ready yet.") from None


@app.post("/backtest", status_code=202)
async def backtest_start(request: Request):
    body = await _body(request)
    strategies = body.get("strategies", list(ctl.STRATEGIES))
    if not isinstance(strategies, list) or not strategies or any(s not in ctl.STRATEGIES for s in strategies):
        raise ctl.ControlError(400, "invalid_strategies", "strategies must be a list drawn from: bands, forecast, peak.")
    year = body.get("year", (RAW.get("backtest") or {}).get("year", 2025))
    if isinstance(year, bool) or not isinstance(year, int):
        raise ctl.ControlError(400, "invalid_year", "year must be a whole number such as 2025.")
    svc = _bridge("src.backtest.service")
    job_id = await run_in_threadpool(svc.start_backtest, strategies, year)
    return JSONResponse(status_code=202, content={"job_id": job_id})


@app.get("/backtest/{job_id}")
async def backtest_job(job_id: str):
    svc = _bridge("src.backtest.service")
    job = await run_in_threadpool(svc.get_job, job_id)
    if not job:
        raise ctl.ControlError(404, "unknown_job", "There's no full-year run with that ID.")
    if job.get("status") == "done":
        try:
            await run_in_threadpool(backend().emit, "backtest_done", "good", "The full-year results are ready.",
                                    detail={"job_id": job_id, "run_ids": job.get("run_ids")},
                                    once_key=f"backtest:{job_id}")
        except Exception:
            pass
    return job


@app.get("/results/pitch")
async def results_pitch():
    svc = _bridge("src.backtest.service")
    result = await run_in_threadpool(svc.pitch_results)
    if not result:
        raise ctl.ControlError(404, "no_results", "No full-year results yet. Run the full year first.")
    return result


@app.get("/forecast")
async def forecast(location: str, at_utc: str | None = None):
    if location not in CFG.fleet.locations:
        raise ctl.ControlError(400, "invalid_location", f"Unknown area {location!r}. Use one of: {', '.join(CFG.fleet.locations)}.")
    provider = _bridge("src.forecast.provider")
    if at_utc:
        try:
            at = ctl.parse_utc(at_utc)
        except ValueError:
            raise ctl.ControlError(400, "invalid_time", "at_utc must be an ISO-8601 time with a zone, e.g. 2025-05-16T21:00:00Z.") from None
    else:
        snap = await _call(lambda: backend().snapshot())
        at = ctl.parse_utc(snap["sim_time_utc"]) if snap.get("sim_time_utc") else datetime.now(UTC)
    result = await run_in_threadpool(provider.latest_forecast, location, at)
    if not result:
        raise ctl.ControlError(404, "no_forecast", "There's no forecast for that area and time yet.")
    return result


# --- WebSocket ---------------------------------------------------------------------------------------


@app.websocket("/live")
async def live(ws: WebSocket):
    await ws.accept()
    closed = asyncio.Event()

    async def drain():  # notice the client going away (and ignore anything it sends)
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except Exception:
            pass
        closed.set()

    reader = asyncio.create_task(drain())
    cursor = LiveCursor()
    try:
        try:
            msg = await run_in_threadpool(snapshot_message, backend(), cursor)
        except Exception:
            msg = _fallback_snapshot()
        await ws.send_text(json.dumps(msg))
        interval = frame_interval_s()
        while not closed.is_set():
            try:
                await asyncio.wait_for(closed.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            try:
                msg = await run_in_threadpool(frame_message, backend(), cursor)
            except Exception:
                msg = {**_fallback_snapshot(), "type": "idle", "ticks": [], "sim_time_utc": None, "speed_tps": None}
                for key in ("history", "settings", "forecast"):
                    msg.pop(key, None)
            await ws.send_text(json.dumps(msg))
    except Exception:
        pass
    finally:
        reader.cancel()
        try:
            await ws.close()
        except Exception:
            pass


def _fallback_snapshot() -> dict:
    """What viewers get when Redis is unreachable: an idle fleet, so the page still renders."""
    idle = backend().idle_snapshot()
    return {"type": "snapshot", "run_id": None, "status": "idle", "settings": idle["settings"], "history": [],
            "totals": idle["totals"], "workers": [], "failed_commands": {"count": 0, "recent": []},
            "availability": idle["availability"], "events": [], "forecast": {}}
