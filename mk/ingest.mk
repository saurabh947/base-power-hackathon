# Ingest / data-layer targets (owner: ingest, milestone M1). Included by the Makefile.
#
# Needs Postgres: DATABASE_URL, or the project-local services
# (PGPORT=5433 scripts/dev/services.sh up). Without DATABASE_URL these targets use
# postgresql://postgres@localhost:$PGPORT/fleetsim, where PGPORT defaults to 5433 like services.sh.
#   make ingest                                   # ERCOT 2024-2026 -> Postgres, gap report, M1 self-check
#   make ingest INGEST_ARGS="--cache-dir data/raw"  # dev: keep and reuse the downloads (data/raw is gitignored)
#   make gap-report                               # report + self-check on what is stored; no download
#   make preset-check                             # check config.yaml presets against real prices
#   make ingest-test                              # the data-layer tests, with the database

INGEST_YEARS ?= 2024 2025 2026
INGEST_ARGS ?=
INGEST_DB = $${DATABASE_URL:-postgresql://postgres@localhost:$${PGPORT:-5433}/fleetsim}

.PHONY: ingest gap-report preset-check ingest-test

# Download, parse and load RT + DA prices, native load and fuel mix. Exits 1 unless the self-check passes.
ingest:
	DATABASE_URL="$(INGEST_DB)" $(UV) run python -m src.ingest.ercot --years $(INGEST_YEARS) $(INGEST_ARGS)

# Gap report and M1 self-check over the stored data (also written to docs/notes/data.md).
gap-report:
	DATABASE_URL="$(INGEST_DB)" $(UV) run python -m src.ingest.report --years $(INGEST_YEARS)

# Presets vs real LZ_HOUSTON / LZ_NORTH prices, plus the recommended ordinary day (into docs/notes/data.md).
preset-check:
	DATABASE_URL="$(INGEST_DB)" $(UV) run python -m src.ingest.presets

# Parser, source, store, series and replay-feed tests, plus the PriceFeed contract suite, against the database.
ingest-test:
	DATABASE_URL="$(INGEST_DB)" $(UV) run pytest tests/test_ingest_*.py tests/test_series_*.py tests/test_contracts_pricefeed.py
