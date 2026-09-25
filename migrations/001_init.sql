-- 001_init: spec section 5.2 tables plus agreed additions. Never edit this file;
-- add migrations/0N0_<area>_<name>.sql instead (010 ingest, 020 backtest, 030 forecast, 040 api).

CREATE TABLE IF NOT EXISTS schema_migrations (
  name text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

-- Spec 5.2. ts = interval START in UTC. RT rows are native 15-min SPP; forward-fill happens in the feed.
CREATE TABLE IF NOT EXISTS prices (
  ts timestamptz NOT NULL,
  location text NOT NULL,
  market text NOT NULL CHECK (market IN ('RT','DA')),
  price_usd_mwh double precision NOT NULL,
  demand_mw double precision,
  fuel_mix jsonb,
  PRIMARY KEY (ts, location, market)
);

-- Spec 5.2 columns first, then agreed additions (schema is not frozen).
CREATE TABLE IF NOT EXISTS backtest_runs (
  run_id uuid PRIMARY KEY,
  started_at timestamptz,
  params jsonb,
  fleet_total_usd double precision,
  per_battery_avg_usd double precision,
  per_location jsonb,
  notes text,
  finished_at timestamptz,
  strategy text,
  top_days jsonb,
  metrics jsonb,
  is_pitch boolean NOT NULL DEFAULT false
);

-- Cached forecasts. Only data strictly before issued_at may be used to make one (no look-ahead).
-- Point i is at start_ts + i * step_minutes.
CREATE TABLE IF NOT EXISTS forecasts (
  issued_at timestamptz NOT NULL,
  location text NOT NULL,          -- settlement point, or 'ERCOT' for system demand
  series text NOT NULL CHECK (series IN ('rt_price','demand')),
  model text NOT NULL,             -- e.g. 'timesfm-3.0'
  start_ts timestamptz NOT NULL,
  step_minutes integer NOT NULL,
  p10 real[] NOT NULL,
  p50 real[] NOT NULL,
  p90 real[] NOT NULL,
  PRIMARY KEY (issued_at, location, series, model)
);
