"""Full-year backtest (spec milestone M4) and the numbers on the website's Results screen.

- runner.py:  vectorized in-process replay of the whole fleet, plus the CLI
              (python -m src.backtest.runner --strategies bands,peak --year 2025).
- ceiling.py: "Best possible (perfect hindsight)", a per-day LP.
- metrics.py: run records (what backtest_runs and the JSON export hold).
- store.py:   persistence to Postgres backtest_runs and to data/results/*.json.
- results.py: the Results object of docs/API.md.
- service.py: the functions the API calls (start_backtest, get_job, pitch_results).
"""
