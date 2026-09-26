# Forecasting: TimesFM-3 and the `forecast` strategy

Owner: forecast builder. Written 2026-09-25. All numbers below were measured on real ERCOT 2025 data
(`data/snapshot`) on the dev laptop (M3 Pro, 12 cores, CPU torch). Nothing here is invented.

## What exists

| Piece | Where | What it does |
|---|---|---|
| Model loader | `src/forecast/model.py` | TimesFM 3.0 (default) or 2.5 behind `forecast.model` / env `FORECAST_MODEL`. CPU torch, weights pinned to a Hugging Face revision, cached in `models/`. |
| Inputs | `src/forecast/inputs.py` | Context windows with strict no look-ahead; DA prices as a known-future covariate, only once published. |
| Job | `src/forecast/job.py` | `python -m src.forecast.job --year 2025 [...]`: hourly rolling-origin forecasts, RT price per location (15-min, 24 h, p10/p50/p90) and ERCOT demand (hourly, 48 h). Resumable, chunked writes, Cloud Run task sharding. |
| Store | `src/forecast/store.py` | The `forecasts` table (001_init, unchanged) or parquet under `data/cache/forecasts/`. |
| Provider | `src/forecast/provider.py` | `latest_forecast(location, at_utc, series="rt_price")` returns the docs/API.md Forecast dict; `ForecastBook` does per-tick lookups for strategies and backtests. No torch import. |
| Strategy | `src/strategy/forecast.py` | `ForecastStrategy`: a rank rule over the cached forecasts. It is registered as `forecast` in `make_strategy` and passes the Strategy contract suite. |
| Accuracy | `src/forecast/accuracy.py` | MAE, pinball loss and 80% coverage against seasonal-naive baselines. |
| Evaluation | `src/forecast/evaluate.py` | A small-fleet loop on real prices comparing forecast, bands and peak. It uses the project's physics, allocation, decision lag and ledger, and reproduces the M0 skeleton's peak number ($4.4470) exactly. |
| Benchmark | `src/forecast/bench.py` | CPU throughput and a projection of the full-year job time. |
| Container | `Dockerfile.forecast` | python:3.12-slim, CPU torch wheel, weights baked in, entrypoint `python -m src.forecast.job`. |
| Prediction | `src/forecast/predict.py`, `predictions.py` | The AI's prediction for the rest of a year from its real_until_utc (chained 7-day chunks), table `price_predictions`; virtual hourly forecasts sliced from it. See "2026" below. |

## How to run

```sh
make setup && make forecast-setup    # main deps, then the optional `forecast` extra (CPU torch 2.14, timesfm 3.0.2)
make forecast-weights                # both pinned checkpoints into models/ (1.3 GB + 0.9 GB, no token)
make forecast-dry-run                # full-year plan and projected compute; loads no model
make forecast-sample                 # 1 day of forecasts to data/cache/forecasts (~5 s)
make forecast-windows                # the 152 evaluation days (~3.5 min on the M3 Pro)
make forecast-accuracy               # MAE / pinball vs seasonal-naive on whatever is cached
make forecast-eval                   # forecast vs bands vs peak on the windows
make forecast-bench                  # tokens/s and the projected cloud time
make test                            # everything; model tests run only when weights are present
```

- **Note:** `make setup` runs an exact `uv sync`, which removes the forecast extra. Run `make forecast-setup` again after it.
- **Database:** `DATABASE_URL=... python -m src.forecast.job --year 2025 --migrate` writes to Postgres.
- **Inputs:** if `src/data/series_pg.py` or the prices table isn't ready, add `SERIES_BACKEND=snapshot` to read the committed real snapshot instead.

## Model and licence

- **timesfm-3.0** (default): `google/timesfm-3.0-pytorch` at revision `43046b85`, 330M parameters, deciles out.
  - The API is new in 3.0: `timesfm3.torch.TimesFM3Forecaster.predict_batch(contexts, horizon, past_future_covariates=...)`. I read it from the installed package before coding.
  - **Licence:** the weights are under the TimesFM Non-Commercial License v1.0, so non-commercial, non-production use only. The human OK'd this for the demo.
- **timesfm-2.5**: `google/timesfm-2.5-200m-pytorch` at revision `1d952420`, Apache-2.0. This is what production would use.
  - To switch, set env `FORECAST_MODEL=timesfm-2.5` or `forecast.model: timesfm-2.5`. No rebuild is needed, because the image bakes both.
  - My 2.5 wrapper is univariate: 2.5's covariates need the XReg/JAX path. The job falls back to univariate with a warning, so the one variable is enough. Verified end to end.
  - Set the same `FORECAST_MODEL` on the API service so it reads the matching rows. The `model` column tells the models apart.
- **Accuracy:** 2.5 is as good as 3.0 univariate at context 1024 (table below). The DA covariate, which only 3.0 takes natively here, is what makes 3.0 better.

**Weights in the container: baked in.** The image downloads both pinned checkpoints at build time from Hugging Face.
- **Why:**
  - Every job run starts in seconds with no network dependency on Hugging Face (unauthenticated downloads are rate-limited).
  - A re-run forecasts with exactly the same weights.
  - The switch to 2.5 works offline.
- **Cost:** about 2.2 GB of image. That is irrelevant for a one-off batch job.
- **Fallback:** a model not baked (build arg `BAKE_MODELS`) downloads at start, because `forecast.download: true`.
- `.dockerignore` and `.gcloudignore` exclude `models/`, so the laptop's copy is never uploaded.

## Benchmark and the chosen plan

Cost scales with transformer tokens: one token per 32-point patch per variate, plus about 3 horizon patches. At 330M parameters that is 0.66 GFLOP per token.

| Local, TimesFM 3.0, fp32 | ms / forecast | tokens / s | GFLOP/s |
|---|---|---|---|
| context 1024, batch 16 | 27.1 | 1,294 | 854 |
| context 1024, batch 64 | 21.9 | 1,600 | 1,056 |
| context 2048, batch 32 | 41.3 | 1,623 | 1,072 |
| job, final plan (ctx 512 + DA), full batches | 27.7 (36 forecasts/s) | ~1,370 | ~900 |

- **The laptop is not a guide to the cloud box.** It hits about 1 TFLOP/s because torch on macOS uses Apple's AMX through Accelerate; plain fp32 GEMM measures 1.5 TFLOP/s.
- **Cloud estimate:** a Cloud Run 8-vCPU box is 4 physical x86 cores. I estimate it at 150–300 GFLOP/s for this model, so the plan was sized from counted FLOPs, not laptop wall time.

**Plan (config.yaml `forecast:`).**
- **RT price:** issued hourly, context 512 (5.3 days) with the DA covariate.
- **Demand:** issued hourly, context 336 (14 days).
- **Size:** 26,280 forecasts (8,760 × 3), 779,640 tokens, about **515 TFLOP**.
- **Projection:** about **29 min at 300 GFLOP/s and 57 min at 150 GFLOP/s** on 8 vCPU. The hard limit is 3 h.
- **Progress:** the job logs its measured rate and ETA after every 4-day chunk.
- **If it runs long:** deploy with `--tasks 2`. Days split by `CLOUD_RUN_TASK_INDEX`/`CLOUD_RUN_TASK_COUNT`, and a re-run skips what is stored.
- **Memory:** the model needs about 2–3 GB, so 32 GiB is plenty.

Why this plan (token cost per forecast in brackets):
- **Hourly issues:** the strategy looks 12 h ahead and short-lead accuracy is by far the best (0–1 h MAE is roughly half the 6–24 h MAE), so fresh forecasts matter more than long context.
- **DA covariate:** it doubles the tokens (2 variates) but cut MAE by 18%.
- **Context:** doubling it only buys about 3%.
- **Result:** ctx 512 + DA [38 tokens] is more accurate than univariate ctx 2048 [67 tokens] at about 55% of the cost. ctx 1024 + DA [70 tokens] is 3% better still, but twice the price.

## No look-ahead

Enforced in `inputs.py` and tested in `tests/test_forecast_inputs.py` and `tests/test_forecast_job.py`:
- **Context:** a forecast issued at T uses only intervals with start + duration <= T. The RT interval [ts, ts+15 min) counts once it has ended, which is when its price is published. The same rule applies to hourly load.
  - Tests poison every interval that has not ended and check the context is unchanged.
  - An end-to-end test with a fake model proves that the last value fed to the model is the price of the interval that ended at T.
- **DA covariate:** day D's DA prices count as known only from 13:30 CT on D-1 (`da_publish_local`). An unpublished horizon hour takes the DA price 24 h earlier.
  - A test poisons unpublished DA prices and checks the covariate is unchanged.
  - DST days are handled with calendar-day arithmetic, which fixed a bug the fall-back test caught. That bug was only ever conservative, never a leak.
- **Strategy:** it uses only forecasts with issued_at <= the decision time. Its price is `decision_tick`'s last *published* price (the 3-tick lag).

## Accuracy

**Ablation.** The sample is 303 issue times per location every 29 h through 2025, so each run sees every hour of the day in every season. Every run uses the same issue times, and each has 29,000 scored points per location.
- **Baselines:**
  - SN1 is the price at the same time yesterday. It is a point forecast, so its pinball loss is half its MAE.
  - SN7 is the p10/p50/p90 of the same time of day over the last 7 days.
- **Units:** MAE and pinball are in $/MWh.

| Run (RT price) | tokens | Houston MAE | North MAE | Houston pinball | North pinball | p10–p90 coverage |
|---|---|---|---|---|---|---|
| SN1: same time yesterday | – | 16.10 | 18.64 | 8.05 | 9.32 | – |
| SN7: 7-day same-time quantiles | – | 13.12 | 14.48 | 4.87 | 5.42 | – |
| 3.0, ctx 512 | 19 | 12.43 | 13.63 | 4.43 | 4.87 | 76% / 75% |
| 3.0, ctx 1024 | 35 | 12.11 | 13.30 | 4.31 | 4.69 | 76% / 76% |
| 3.0, ctx 2048 | 67 | 11.76 | 12.89 | 4.15 | 4.51 | 77% / 78% |
| **3.0, ctx 512 + DA (chosen)** | 38 | **10.18** | **10.99** | **3.60** | **3.96** | 72% / 72% |
| 3.0, ctx 1024 + DA | 70 | 9.88 | 10.86 | 3.49 | 3.85 | 75% / 74% |
| 2.5, ctx 1024 (univariate) | ~64 | 12.05 | 13.45 | 4.31 | 4.79 | 76% / 77% |

The DA covariate helps at every lead. Houston MAE at 0–1 / 1–6 / 6–24 h:
- ctx 512 + DA: 5.18 / 8.48 / 10.94
- ctx 512 univariate: 5.44 / 10.75 / 13.28

**Chosen configuration, 152 evaluation days, hourly issues.** 3,649 forecasts per location, 350,304 scored points each:

| | MAE | SN1 MAE | skill vs SN1 | pinball | SN7 pinball | skill vs SN7 | coverage |
|---|---|---|---|---|---|---|---|
| LZ_HOUSTON | 11.73 | 18.62 | +37% | 4.24 | 5.59 | +24% | 72% |
| LZ_NORTH | 12.79 | 20.85 | +39% | 4.76 | 6.16 | +23% | 72% |
| LZ_HOUSTON, lead 0–1 h | 7.17 | 18.69 | +62% | 2.51 | 5.57 | +55% | 76% |
| LZ_HOUSTON, lead 6–24 h | 12.33 | 18.60 | +34% | 4.47 | 5.60 | +20% | 72% |

**ERCOT demand**, 300 sampled 48-h forecasts, MAE in MW:
- **Context 336 (chosen):** MAE 2,626 against SN1 3,058 (+14%). Pinball 853 against SN7 1,253 (+32%).
- **Context 512:** 2,590. **Context 256:** 2,676.
- **Lead 0–1 h:** 311 MW.

**Caveats.**
- The p10–p90 band covers only 72–76% of outcomes against a nominal 80%. It is too narrow, mostly on spikes, so the site's band understates uncertainty.
- The p50 almost never foresees a spike. On 2025-05-16 the 4 pm forecast had p50 of about $75–105 and p90 of about $160–195 for the evening; the real price reached $2,250.

## The `forecast` strategy

**The rule** (`src/strategy/forecast.py`; a rule, not a solver).
- **Inputs:** the decision price p (the last *published* price) and the latest forecast issued at or before the decision time.
- **Window:** the p50 of the forecast points in the next **12 h**, three 5-minute slots per 15-minute point.
- **k:** the number of 5-minute slots at full power that empty a full battery (k_sell) or fill an empty one (k_buy). It comes from the fleet's energy-to-power ratio, which is passed through the constructor. For the configured 20k fleet: 1.82 h, k_sell 17, k_buy 19.
- **Sell** if p > 0 and fewer than k_sell slots ahead are forecast higher.
- **Buy** if fewer than k_buy slots ahead are forecast lower, and p <= 0.9 × (the price of the k_sell-th best slot), so the round trip pays for its losses.
- **Otherwise hold.** Also hold if there is no forecast, or it is more than 6 h old.
- **Honest labels:** an empty fleet never "sells" and a full one never "buys", so the UI's action label is honest.

**Evaluation.**
- **Setup:**
  - 152 real 2025 days in 4 windows chosen to include every big spike day (01-15, 04-07, 05-08, 05-16, 05-23, 05-30, 07-11, 10-21, 11-07).
  - 100 seeded batteries, 50 per location, one strategy instance per location.
  - Start SoC 0.5, decision lag 3 ticks.
  - Same physics, allocation and ledger as the backtest, and the same forecasts the cloud job makes.
- **Units:** $ per battery over each window (not annualized). Cycles are equivalent full cycles per battery over the 152 days.

| Strategy | Jan 6–Feb 4 (30 d) | Apr 1–May 31 (61 d) | Jul (31 d) | Oct 15–Nov 13 (30 d) | Total | vs bands | Cycles |
|---|---|---|---|---|---|---|---|
| bands (v1) | $5.99 | $23.94 | $10.44 | $10.93 | $51.31 | – | 71 |
| peak | $6.29 | $24.50 | $10.56 | $15.73 | $57.08 | +11% | 144 |
| **forecast (TimesFM-3)** | **$9.50** | **$34.93** | **$16.45** | **$17.11** | **$78.00** | **+52%** | 138 |
| same rule, fed yesterday's prices | $7.10 | $24.16 | $12.70 | $10.91 | $54.87 | +7% | 103 |
| same rule, fed the actual future (oracle, peeks) | $22.57 | $73.25 | $22.43 | $21.26 | $139.52 | +172% | 144 |

**Reading it.**
- The forecast strategy beats both bands and peak in every window. Overall it earns +52% over bands and +37% over peak, while cycling slightly less than peak.
- Fed "same time yesterday", the identical rule earns only $54.87. The forecast is doing the work, not the rule alone.
- The oracle row shows how much is left on the table: the gap is forecast quality, mostly spikes.

**How the rule was chosen.** All variants were scored on the same 152 days, so these parameters are **in-sample**. Details:
- **Horizon** (full-battery k):

  | Horizon | 4 h | 6 h | 8 h | 10 h | 12 h | 16 h | 24 h |
  |---|---|---|---|---|---|---|---|
  | $ per battery | 87.06 | 86.60 | 83.88 | 81.45 | 78.00 | 72.50 | 63.09 |
  | Cycles | 290 | 229 | 182 | 157 | 138 | 124 | – |

  - Shorter windows earn more but trade more often.
  - **12 h is the default** because it cycles like the peak rule (about 0.9 a day), so it wins on timing rather than extra wear. Battery wear is excluded from the pitch number.
  - 4 h earns +12% more at twice the cycling. That is the human's call if wear is priced in later.
- **k mode:** "full-battery k" (the task's example) beats "k from current SoC" at every horizon, by 2–7% (12 h: $78.00 vs $75.51).
- **Charge guard:** it almost never binds (12 h without it: $77.97). I kept it because it is principled: never buy without an expected sale that covers the losses.
- **p90 spike reserve**, kept as an option (`reserve_spike_usd`) and off by default:
  - Across settings it ranged from -6% to +5% in total.
  - It helps in spike season (Apr–May up to $43.96 vs $34.93) and hurts the other three windows.
  - It is inconsistent, so it stays off.
- **Graded power**, scaling power with rank depth: -3 to -4%. Not used.

**Demo day** (`python -m src.sim.skeleton --strategy forecast`: 1 battery, LZ_HOUSTON, 2025-05-16):
- **Results:** bands $0.51, peak $4.45, forecast $1.32.
- **Why:** the forecast rule filled up in the morning and sold at 15:00–16:00, when $86–150 beat every p50 of the next 12 h. It was empty for the 19:00 hour ($1,507 average, $2,250 peak).
- **Takeaway:** the peak rule's fixed 18:00–21:00 window wins this particular evening. Expect the live demo to show that honestly.

**Caveats.**
- **In-sample:** the horizon and k mode were picked on these 152 days. The full-year backtest after the cloud job is the out-of-sample check for the other 213 days.
- **Spike-heavy windows:** 61 of the 152 days are April–May. **Don't annualize** $78.00 to a pitch number; use the backtest.
- **Performance:** construction takes 0.2 s (books for both locations plus the fleet). `target_kw` costs 8 µs per tick, about 1.7 s for a full year across both locations.

## Using it from other areas

- **API** `GET /forecast?location=&at_utc=`: `from src.forecast.provider import latest_forecast`. It returns the Forecast dict, or `None` (send 404).
  - It reads Postgres when `DB_HOST`/`DATABASE_URL` is set; one query takes about 1 ms. A Forecast is about 7.6 KB of JSON.
  - Demand uses `latest_forecast("ERCOT", at, series="demand")`.
- **Coordinator / backtest:** `make_strategy(cfg.strategy, tick_seconds, "forecast", batteries=<that location's cohort>)`, one instance per location.
  - `books=` defaults to `ForecastBook.load(...)` for each configured location.
  - Call it through `decision_tick` like the other strategies. It ignores `price_history`.
- **The API image stays small:** torch and timesfm live only in the optional `forecast` group. The strategy, provider, store, accuracy and evaluation modules never import torch (verified).

## 2026: the AI's prediction for the rest of the year (2026-09-26)

2026 is published year-to-date. Its real prices end at `real_until_utc` (src/data/years.py: the end of the last RT
interval every fleet city has; 2026-09-20 05:00Z with the files ERCOT published on Sep 20). After it, the replay
runs on the AI's prediction.

### The prediction job (`src/forecast/predict.py`)

```sh
python -m src.forecast.predict --year 2026 [--out db|parquet] [--limit-days N] [--chunk-days 7] [--context N]
make forecast-predict                     # locally (parquet, or the DB when DATABASE_URL is set)
scripts/cloud/jobs.sh predict             # Cloud Run Job fleet-sim-predict, forecast image, 8 vCPU / 16 GiB
```

- Per fleet city, from real_until_utc to Dec 31 24:00 Central, TimesFM-3 univariate, chained in 7-day chunks: each
  chunk's p50 is appended to the context (config `forecast.predict.context_steps`: the last 4,096 steps = 42.7 days)
  and the next 7 days are forecast from it. All four cities go through the model in one batched call per chunk:
  15 calls for 2026 (9,892 steps = 103 days + 1 h).
- **No look-ahead:** the context holds only real intervals that ENDED at or before the cutoff; nothing after the
  cutoff is requested, even for a city with more real data than the others. `tests/test_forecast_predict.py` proves
  the chaining and the cutoff with a fake model and poisoned data past the cutoff.
- **Univariate** because the DA covariate is only published a day ahead, never for the months to come.
- **The band:** fed its own smooth p50, TimesFM grows very sure of itself: on the 2026 run the mean p10–p90 width was
  $36/MWh in week 1, $16 in week 2 and $1–4 from week 3. So each later chunk's band is floored at the first week's
  band at the same offset (the same time of week): it never claims more certainty about December than about next
  week. It still doesn't widen with the horizon.
- **Storage:** table `price_predictions` (migration 030): `(model, location, cutoff, ts)` primary key, with
  `issued_at`, `chunk` (0 = saw only real prices), p10/p50/p90 in $/MWh rounded to cents. A re-run with the same
  cutoff replaces that path in one transaction per city (idempotent); a later cutoff (after a newer ERCOT
  publication) adds a new path, and readers take the latest cutoff at or before the real_until they replay against.
  `--out parquet` writes `data/cache/predictions/{model}/{location}/{cutoff}.parquet` (gitignored), the dev path.
- **Weights:** as src/forecast/model.py (pinned revisions; baked into the forecast image; `FORECAST_MODEL` switches).
- **Local run (M3 Pro, CPU torch, 12 threads):** the whole 2026 remainder for 4 cities in **10–11 s** (model 7.7 s
  for 15 calls, weights load 1.3–2.4 s); a 7-day sample (`--limit-days 7`, 1 call) in 9.2 s wall, 0.6 s of model.
  The cloud's 8 vCPU box should take well under a minute.
- **What it predicted (cutoff 2026-09-20):** p50 means Houston $34.51, Dallas–Fort Worth $31.24, San Antonio $36.07,
  Austin $35.54/MWh; p50 ranges $12–86. A smooth daily wave, no spikes (it is a median).
- **Out of sample, the same method on 2025** (cut at 2025-09-20, compared with the real Q4 2025):

  | City | real mean | p50 mean | MAE p50 | MAE "last week repeated" | p10–p90 coverage | real sd | p50 sd |
  |---|---:|---:|---:|---:|---:|---:|---:|
  | Houston | 32.37 | 31.48 | 13.35 | 15.28 | 57% | 23.0 | 13.2 |
  | Dallas–Fort Worth | 32.82 | 30.17 | 16.20 | 18.18 | 52% | 29.0 | 14.9 |
  | San Antonio | 39.59 | 34.57 | 21.33 | 21.73 | 51% | 48.1 | 18.4 |
  | Austin | 40.97 | 33.39 | 23.54 | 23.33 | 46% | 57.6 | 19.2 |

  The level is right to within $1–8/MWh, it beats repeating the last week in three cities of four (Austin: a tie),
  and the band covers only about half of what really happened (nominal 80%): real prices spike, the median path
  doesn't.

### Virtual forecasts after the cutoff (`src/forecast/predictions.py`, `provider.py`)

- After real_until_utc there are no real prices to forecast from. The rolling job (`src.forecast.job --year 2026`)
  now stops at real_until_utc (issue times before it only), and skips any issue time past the end of a series' own
  real data (2026 load ends Aug 31: no demand forecasts after it), so nothing is forecast from stale context.
- Instead the AI strategy gets **virtual forecasts**: one per whole hour from the cutoff, each the next 24 h (96
  points) of the stored prediction's p10/p50/p90 (NaN past its end). `ForecastBook.load(..., virtual=True)` (what
  `make_forecast_strategy` asks for) drops any real forecast issued after the cutoff and appends them;
  `latest_forecast` (GET /forecast, the live band) answers from them too. Without `virtual=True` a book is exactly
  what is stored (accuracy, evaluation, tests).
- **The AI sees its own long-range prediction.** After the cutoff the replay's prices ARE the prediction's p50, so
  the strategy's forecast of them is exact by construction: it knows the whole next 24 h perfectly. Its projected
  earnings after real_until_utc therefore flatter it compared with the simple rule and the evening peak, which just
  trade on the predicted prices. That is inherent to "predict the rest of the year, then replay it"; the site labels
  that money as projected (docs/UX.md).

## Open issues

- **Cloud CPU speed is an estimate** (150–300 GFLOP/s). Read the job's first ETA line; if it says over 60 min, redeploy with `--tasks 2` and re-run (it resumes).
- **The 80% band is too narrow** (72% coverage). Conformal widening per lead time would fix it cheaply if the site's band matters.
- **Forecasts miss spikes**, so the strategy sells early on spike evenings. The p90 reserve helped spike season but hurt elsewhere. A better spike signal (DA-RT spread, fuel mix, load forecast) is future work.
- **Demand has no 2024 load in the snapshot**, so in snapshot mode the first 72 h of 2025 are skipped (`min_context_steps`).
- **The Docker image was not built locally** (heavy). I verified its moving parts: the export and CUDA filter (54 packages kept, 20 CUDA-related dropped), `torch==2.14.0+cpu` exists for cp312 x86_64 on download.pytorch.org/whl/cpu, and the weight-baking snippet runs.
