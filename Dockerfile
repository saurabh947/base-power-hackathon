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
COPY src ./src
ENV PATH="/app/.venv/bin:$PATH" START_REDIS=1 REDIS_URL=redis://127.0.0.1:6379/0
# tini reaps zombies (SIGKILLed workers) and forwards SIGTERM from Cloud Run
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "src.launcher"]
