# Full-year backtest (spec M4) and the Results screen's numbers. Owner: backtest.
# See docs/notes/backtest.md. Included by the Makefile (which defines UV).
#
#   make backtest                          config backtest.strategies for config backtest.year
#   make backtest BT_STRATEGIES=bands,peak BT_YEAR=2025 BT_ARGS=--no-db
#   make backtest-pitch                    ... and mark the runs as the Results screen's set
#   make backtest-speed                    full-year x 20,000-battery speed + identity tests (1-2 min)
#   make backtest-test-pg                  the Postgres tests; needs DATABASE_URL (scripts/dev/services.sh up)
#
# Writes JSON to data/results/ (or $BACKTEST_RESULTS_DIR); also writes backtest_runs when
# DATABASE_URL or DB_HOST is set, unless BT_ARGS contains --no-db.

BT_STRATEGIES ?=
BT_YEAR ?=
BT_ARGS ?=
BT_OPTS = $(if $(BT_STRATEGIES),--strategies $(BT_STRATEGIES)) $(if $(BT_YEAR),--year $(BT_YEAR)) $(BT_ARGS)

.PHONY: backtest backtest-pitch backtest-speed backtest-test-pg

backtest:
	$(UV) run python -m src.backtest.runner $(BT_OPTS)

backtest-pitch:
	$(UV) run python -m src.backtest.runner --pitch $(BT_OPTS)

backtest-speed:
	BACKTEST_SPEED=1 $(UV) run pytest tests/test_backtest_speed.py -q -s

backtest-test-pg:
	@test -n "$$DATABASE_URL" || { echo "set DATABASE_URL (PGPORT=... scripts/dev/services.sh up prints it)"; exit 2; }
	$(UV) run pytest tests/test_backtest_store_pg.py -q -rs
