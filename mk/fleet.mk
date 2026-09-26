# Orchestrated fleet (M2/M3) + backend API. Owner: fleet. Included by the root Makefile.
# Services run on the fleet's own ports so parallel worktrees never collide.
FLEET_PGPORT ?= 5442
FLEET_REDIS_PORT ?= 6442
FLEET_REDIS_URL ?= redis://localhost:$(FLEET_REDIS_PORT)/0
FLEET_API_PORT ?= 8442
FLEET_TESTS = tests/test_fleet_*.py tests/test_recovery.py tests/test_idempotency.py tests/test_api_*.py \
              tests/test_redis_*.py tests/test_contracts_battery.py

.PHONY: fleet-up fleet-down fleet-test fleet-test-all fleet-m2 fleet-m3 fleet-m3-2tps fleet-serve

# Project-local Postgres + Redis on the fleet ports (prints DATABASE_URL / REDIS_URL).
fleet-up:
	PGPORT=$(FLEET_PGPORT) REDIS_PORT=$(FLEET_REDIS_PORT) scripts/dev/services.sh up

fleet-down:
	PGPORT=$(FLEET_PGPORT) REDIS_PORT=$(FLEET_REDIS_PORT) scripts/dev/services.sh down

# Fleet, recovery, idempotency, API and adapter suites against the fleet Redis (~1 min).
fleet-test:
	REDIS_URL=$(FLEET_REDIS_URL) $(UV) run pytest $(FLEET_TESTS)

# The whole suite with Redis reachable, so nothing redis-marked is skipped.
fleet-test-all:
	REDIS_URL=$(FLEET_REDIS_URL) $(UV) run pytest

# M2: 1 sim day (288 ticks), 20,000 batteries, 20 worker processes, max speed. Prints ticks/s and max error.
fleet-m2:
	REDIS_URL=$(FLEET_REDIS_URL) $(UV) run python -m src.sim.coordinator bench --ticks 288 --speed max --strategy bands

# M3: SIGKILL worker 7 during max discharge at max speed; prints error % per tick after the kill.
fleet-m3:
	REDIS_URL=$(FLEET_REDIS_URL) $(UV) run python -m src.sim.coordinator bench --ticks 24 --speed max \
	  --strategy peak --start-utc 2025-05-16T22:50:00Z --soc 0.9 --kill-at 6 --kill-worker 7

# M3 at the demo speed, 2 ticks/s (~12 s).
fleet-m3-2tps:
	REDIS_URL=$(FLEET_REDIS_URL) $(UV) run python -m src.sim.coordinator bench --ticks 24 --speed 2 \
	  --strategy peak --start-utc 2025-05-16T22:50:00Z --soc 0.9 --kill-at 6 --kill-worker 7

# The whole backend (coordinator + 20 workers + API on :$(FLEET_API_PORT)) against the fleet Redis.
# Presenter mode needs PRESENTER_PASSCODE (dev) or a passcode file at PRESENTER_PASSCODE_FILE.
fleet-serve:
	REDIS_URL=$(FLEET_REDIS_URL) PORT=$(FLEET_API_PORT) $(UV) run python -m src.launcher
