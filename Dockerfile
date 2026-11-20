# fleet-sim backend: API + coordinator + 20 worker processes + in-container Redis (Cloud Run, gen2).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN apt-get update \
 && apt-get install -y --no-install-recommends redis-server tini \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.9.13
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY config.yaml ./
COPY migrations ./migrations
COPY data/snapshot ./data/snapshot
COPY data/frozen ./data/frozen
COPY src ./src
# No database: prices come from data/snapshot, and the AI forecasts, the 2026 prediction and the full-year results
# from data/frozen (exported from Cloud SQL before it was deleted; src/data/freeze.py). The results are fixed, so
# full-year re-runs are off.
ENV PATH="/app/.venv/bin:$PATH" START_REDIS=1 REDIS_URL=redis://127.0.0.1:6379/0 \
    PRICE_SOURCE=snapshot FORECAST_CACHE_DIR=/app/data/frozen/forecasts \
    PREDICTION_CACHE_DIR=/app/data/frozen/predictions BACKTEST_RESULTS_DIR=/app/data/frozen/results \
    BACKTEST_RERUNS=off
# tini reaps zombies (SIGKILLed workers) and forwards SIGTERM from Cloud Run
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "src.launcher"]
