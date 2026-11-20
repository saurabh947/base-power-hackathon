# Frozen data

What the backend used to read from its Cloud SQL database, as files. The backend image serves them
(Dockerfile: `FORECAST_CACHE_DIR`, `PREDICTION_CACHE_DIR`, `BACKTEST_RESULTS_DIR`), so it runs without a
database. Prices come from `data/snapshot`, which holds the same ERCOT publication the database did (checked
row by row for 2024–2026, real-time and day-ahead).

Exported on 2026-11-19 from Cloud SQL instance `fleet-sim-db` by `src/data/freeze.py`, which read every file back
and compared it with the database. The instance was then deleted. Its full SQL export is kept privately at
`gs://appintellect-fleet-sim-backup/fleetsim-2026-11-19.sql.gz`.

**Only `results/` is in git.** `forecasts/` and `predictions/` are TimesFM-3 output (about 100 MB, and the model's
weights are licensed for non-commercial use only), so they're kept in the private backup bucket instead. Copy them
in before building the backend image; `scripts/cloud/deploy.sh` refuses to deploy without them:

```sh
gcloud storage cp -r gs://appintellect-fleet-sim-backup/frozen/forecasts gs://appintellect-fleet-sim-backup/frozen/predictions data/frozen/
```

| Folder | What | Size |
|---|---|---|
| `forecasts/timesfm-3.0/rt_price/<city>/` | TimesFM-3 rolling price forecasts for the four cities, hourly, from 2024-12-24 to 2026-09-20 | 15,245 per city |
| `forecasts/timesfm-3.0/demand/ERCOT/` | TimesFM-3 rolling ERCOT demand forecasts over the same period (only `GET /forecast?series=demand` uses them) | 15,245 |
| `predictions/timesfm-3.0/<city>/` | The AI's 2026 price prediction from where real prices end (2026-09-20T05:00Z) to Dec 31 | 9,892 points per city |
| `results/` | Every full-year run (`runs/`) and the Results screen's sets for 2025 and 2026 (`pitch_<year>*.json`) | 36 runs |

Checks before the database was deleted:
- Database mode and files mode gave identical answers for the Results, the AI gains, 736 sampled forecasts,
  the 2026 prediction and the forecast books the AI strategy trades on.
- Re-running the 2025 and 2026 full years from these files reproduced the published figures exactly
  (e.g. $190.950711 per battery for the AI forecast in 2025).

The forecasts come from Google's TimesFM-3 research weights, which are licensed for non-commercial use.

## Rebuilding

Restore the SQL export into a local Postgres, then:

```sh
DATABASE_URL=postgresql://postgres@localhost:5442/fleetsim_cloud uv run python -m src.data.freeze --out data/frozen --replace
```

To run the backend locally on this data, start it with the same three variables as the Dockerfile, pointing at
these folders, and no `DATABASE_URL`.
