-- 010_ingest: tables owned by the ERCOT ingest (src/ingest, milestone M1).
-- Never edit this file once it has been applied; add migrations/011_ingest_<name>.sql instead.
--
-- prices (001) holds all 15 ERCOT hubs and load zones:
--   RT rows: native 15-min settlement point prices (report 13061), market = 'RT'.
--   DA rows: hourly settlement point prices (report 13060), market = 'DA'.
--   ts is the interval START in UTC. LZ prices are the plain LZ type, never LZEW.
-- RT rows also carry context for the replay feed (NULL where no data was ingested):
--   demand_mw = ERCOT native load, the hourly average MW of the hour that contains the interval.
--   fuel_mix  = {"biomass", "coal", "gas", "gas-cc", "hydro", "nuclear", "other", "solar", "wind"}:
--               fractions of positive generation in that 15-min interval, WSL excluded
--               (the same numbers as src.data.series.fuel_mix).
-- DA rows leave demand_mw and fuel_mix NULL.

-- ERCOT hourly native load (ercot.com/gridinfo/load/load_hist yearly archives).
CREATE TABLE IF NOT EXISTS native_load (
  ts timestamptz NOT NULL,            -- hour START, UTC
  zone text NOT NULL,                 -- 'ERCOT' (system total) or a weather zone as published:
                                      -- COAST, EAST, FWEST, NORTH, NCENT, SOUTH, SCENT, WEST
  load_mw double precision NOT NULL,  -- hourly average native load, MW
  PRIMARY KEY (zone, ts)
);

-- ERCOT 15-min generation by fuel (IntGenbyFuel{year} workbooks).
CREATE TABLE IF NOT EXISTS generation_by_fuel (
  ts timestamptz NOT NULL,            -- 15-min interval START, UTC
  fuel text NOT NULL,                 -- as published: Biomass, Coal, Gas, Gas-CC, Hydro, Nuclear, Other, Solar, WSL, Wind
  gen_mw double precision NOT NULL,   -- average MW over the interval (the file's MWh x 4); WSL < 0 while storage charges
  share double precision,             -- fraction of positive generation, WSL excluded; NULL for WSL
  settlement_type text,               -- settlement type of the source row, e.g. FINAL
  PRIMARY KEY (ts, fuel)
);

-- Provenance of the last successful load of each (dataset, year).
CREATE TABLE IF NOT EXISTS ingest_files (
  dataset text NOT NULL CHECK (dataset IN ('rt_spp', 'da_spp', 'native_load', 'fuel_mix')),
  year integer NOT NULL,              -- ERCOT (America/Chicago) calendar year
  source_url text NOT NULL,
  source_name text NOT NULL,          -- file name as published (MIS constructed name, or file / zip member)
  published_at timestamptz,           -- MIS PublishDate, or the HTTP Last-Modified header
  sha256 text NOT NULL,               -- of the downloaded file
  bytes bigint NOT NULL,
  rows_loaded integer NOT NULL,
  download_s real,
  parse_s real,
  load_s real,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (dataset, year)
);

-- series_pg and the replay feed read one (location, market) over a time range.
CREATE INDEX IF NOT EXISTS prices_location_market_ts ON prices (location, market, ts) INCLUDE (price_usd_mwh);
