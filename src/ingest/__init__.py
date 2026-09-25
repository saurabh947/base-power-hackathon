"""ERCOT data ingest (milestone M1): public ERCOT files -> Postgres.

- sources: find and download the files (MIS annual zips, ercot.com yearly archives).
- parse:   turn workbooks into tidy UTC frames (pure, no network, no database).
- store:   idempotent upserts into Postgres.
- report:  gap report and the M1 self-check.
- ercot:   the CLI / Cloud Run job entrypoint (python -m src.ingest.ercot).
- presets: checks replay presets against real prices (python -m src.ingest.presets).
"""
