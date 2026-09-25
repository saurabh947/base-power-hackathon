# fleet-sim developer entry points. See RUNLOG.md "How to run".
UV ?= $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

.PHONY: setup test m0

# Create .venv on Python 3.12 and install locked deps (numpy, pyyaml; dev: pytest).
setup:
	@test -d .venv || $(UV) venv --python 3.12
	$(UV) sync

# All tests: seam contract suites (tests/test_contracts_*.py) plus unit and M0 tests.
test:
	$(UV) run pytest

# M0 walking skeleton: 1 battery, real ERCOT RT prices for LZ_HOUSTON on 2025-05-16.
# Exits 1 unless Ledger.summary() reports nonzero USD.
m0:
	$(UV) run python -m src.sim.skeleton

# Area targets (ingest, fleet, backtest, forecast, web, cloud) live in mk/*.mk
-include mk/*.mk
