"""Price and demand forecasting with Google TimesFM (RUNLOG: the human added forecasting).

Modules, lightest first. Only model.py and job.py import torch/timesfm, which
live in the optional `forecast` dependency group; everything else runs on the
main dependencies, so the API and the backtest can read cached forecasts
without the model installed.

- settings.py  the `forecast:` section of config.yaml, typed and validated
- inputs.py    context windows with strict no look-ahead, plus DA covariates
- store.py     the forecasts table (Postgres) and the parquet cache
- provider.py  latest_forecast() and ForecastBook: what callers use
- model.py     TimesFM loader behind the config switch (3.0 default, 2.5 fallback)
- job.py       rolling-origin forecasts for a year: python -m src.forecast.job
- accuracy.py  MAE and pinball loss against seasonal-naive
- evaluate.py  small-fleet comparison of forecast vs bands vs peak on real prices
- bench.py     CPU throughput benchmark
"""
