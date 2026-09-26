# Website targets (web/). See web/README.md. Included by the root Makefile.
NPM ?= npm
WEB_DIR := web

.PHONY: web-install web-dev web-dev-local web-mock web-typecheck web-lint web-test web-build web-check web-prices

# Install the locked dependencies (Node 22.12+).
web-install:
	cd $(WEB_DIR) && $(NPM) ci

# Dev server on http://localhost:5173 with the built-in mock API (simulated; real prices for 2025-05-16/17).
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

# Re-export the mock's real-price fixture from data/snapshot (needs `make setup`).
web-prices:
	$(UV) run python web/mock/export_prices.py
