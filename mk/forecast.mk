# Forecasting (TimesFM). Owner: forecast. Notes: docs/notes/forecast.md
# `make setup` syncs exactly the main deps and so removes torch/timesfm; `make forecast-setup` adds them back.

.PHONY: forecast-setup forecast-weights forecast-test forecast-bench forecast-sample forecast-windows \
        forecast-accuracy forecast-eval forecast-dry-run forecast-year forecast-predict

FORECAST_WINDOWS ?= 2025-01-06:30 2025-04-01:61 2025-07-01:31 2025-10-15:30

# Install the optional `forecast` extra (CPU torch + timesfm) into .venv.
forecast-setup:
	@test -d .venv || $(UV) venv --python 3.12
	$(UV) sync --extra forecast

# Fetch both pinned checkpoints into models/ (1.3 GB + 0.9 GB, no token needed).
forecast-weights:
	$(UV) run --extra forecast python -c "from src.forecast.model import MODELS, ensure_weights; \
	from src.forecast.settings import load_settings; d = load_settings().weights_dir; \
	[print(ensure_weights(m, d)) for m in MODELS.values()]"

# Forecast tests only; the model tests run when the weights are present (~10 s).
forecast-test:
	$(UV) run pytest tests/test_forecast_*.py tests/test_contracts_strategy.py -q

# CPU throughput and the projected full-year job time (~30 s on an M3 Pro).
forecast-bench:
	$(UV) run --extra forecast python -m src.forecast.bench --contexts 512 1024 --batch 32 --n 64 --covariate

# One day of forecasts into the parquet cache (data/cache/forecasts): a quick end-to-end check.
forecast-sample:
	$(UV) run --extra forecast python -m src.forecast.job --year 2025 --start 2025-05-16 --limit-days 1 --out parquet

# Hourly RT forecasts for the evaluation windows (152 days, ~3.5 min on an M3 Pro).
forecast-windows:
	for w in $(FORECAST_WINDOWS); do \
	  $(UV) run --extra forecast python -m src.forecast.job --year 2025 --series rt_price --out parquet \
	    --start $${w%%:*} --limit-days $${w##*:} || exit 1; \
	done

# MAE / pinball vs seasonal-naive on whatever is cached (parquet, or the DB when configured).
forecast-accuracy:
	$(UV) run python -m src.forecast.accuracy --by-lead

# forecast vs bands vs peak (+ naive/oracle references) on the windows, 100 batteries.
forecast-eval:
	$(UV) run python -m src.forecast.evaluate --windows $(FORECAST_WINDOWS)

# The full-year plan: counts and projected compute. No model is loaded.
forecast-dry-run:
	$(UV) run python -m src.forecast.job --year 2025 --dry-run

# The full year, as the Cloud Run job runs it. ~8 min of all-core CPU even on an M3 Pro: run it in the cloud.
forecast-year:
	$(UV) run --extra forecast python -m src.forecast.job --year 2025

# The AI's prediction for the rest of 2026 from where real prices end (~10 s on an M3 Pro; parquet unless a DB is set).
forecast-predict:
	$(UV) run --extra forecast python -m src.forecast.predict --year 2026
