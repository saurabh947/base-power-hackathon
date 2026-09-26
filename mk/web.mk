# Website targets (web/). See web/README.md. Included by the root Makefile.
NPM ?= npm
WEB_DIR := web

.PHONY: web-install web-dev web-dev-local web-mock web-typecheck web-lint web-test web-build web-check web-prices web-texas

# U.S. Census Bureau 2023 cartographic boundary file, states, 1:5,000,000 (public domain).
CENSUS_STATES_URL := https://www2.census.gov/geo/tiger/GENZ2023/kml/cb_2023_us_state_5m.zip

# Install the locked dependencies (Node 22.12+).
web-install:
	cd $(WEB_DIR) && $(NPM) ci

# Dev server on http://localhost:5173 with the built-in mock API (simulated; real prices for the preset days).
web-dev:
	cd $(WEB_DIR) && $(NPM) run dev

# Dev server against a backend on http://localhost:8000.
web-dev-local:
	cd $(WEB_DIR) && $(NPM) run dev:local

# Standalone mock API on http://localhost:8787.
web-mock:
	cd $(WEB_DIR) && $(NPM) run mock

web-typecheck:
	cd $(WEB_DIR) && $(NPM) run typecheck

web-lint:
	cd $(WEB_DIR) && $(NPM) run lint

web-test:
	cd $(WEB_DIR) && $(NPM) test

# Production build into web/dist (set VITE_API_BASE_URL first); fails if mock code reaches dist/.
web-build:
	cd $(WEB_DIR) && $(NPM) run build

# Typecheck, lint, tests and build.
web-check:
	cd $(WEB_DIR) && $(NPM) run check

# Re-export the mock's real-price fixture for every 2025 preset in config.yaml, plus the mock-only days that
# stand in for 2026 (needs `make setup`).
web-prices:
	$(UV) run python web/mock/export_prices.py

# Rebuild web/src/map/texasOutline.ts from the Census file (downloaded to a temp dir, then removed).
web-texas:
	@tmp=$$(mktemp -d) && \
	curl -sSL -o $$tmp/states.zip $(CENSUS_STATES_URL) && \
	unzip -q -o $$tmp/states.zip cb_2023_us_state_5m.kml -d $$tmp && \
	(cd $(WEB_DIR) && node scripts/texas-outline.ts $$tmp/cb_2023_us_state_5m.kml); \
	status=$$?; rm -rf $$tmp; exit $$status
