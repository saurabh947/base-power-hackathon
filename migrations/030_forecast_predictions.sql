-- 030_forecast: the AI's prediction for the rest of a year (src/forecast/predict.py).
-- Never edit this file once it has been applied; add migrations/031_forecast_<name>.sql instead.
--
-- One path per (model, location, cutoff): 15-min RT settlement prices from `cutoff` to the end of the ERCOT
-- (America/Chicago) year, made by TimesFM univariate in chained chunks (each chunk's p50 is fed back as
-- context for the next one). `cutoff` is the year's real_until_utc when the job ran: the model saw only real
-- intervals that ENDED at or before it, so ts >= cutoff for every row. A re-run with the same cutoff replaces
-- that path (one transaction per location); a later cutoff (after a newer ERCOT publication) adds a new path
-- and readers take the latest cutoff at or before the real_until they replay against.

CREATE TABLE IF NOT EXISTS price_predictions (
  model text NOT NULL,                -- e.g. 'timesfm-3.0' (src/forecast/model.py MODELS)
  location text NOT NULL,             -- settlement point, e.g. 'LZ_HOUSTON'
  cutoff timestamptz NOT NULL,        -- real_until_utc the prediction starts from (the end of the last real interval)
  ts timestamptz NOT NULL,            -- 15-min interval START, UTC; ts >= cutoff
  issued_at timestamptz NOT NULL,     -- wall-clock time the job made the path
  chunk smallint NOT NULL,            -- which chained step made the point: 0 saw only real prices
  p10 double precision NOT NULL,      -- $/MWh, rounded to cents
  p50 double precision NOT NULL,      -- the replay's price after the cutoff (docs/API.md Tick.predicted)
  p90 double precision NOT NULL,
  PRIMARY KEY (model, location, cutoff, ts)
);
