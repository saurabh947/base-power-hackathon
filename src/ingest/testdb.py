"""Throwaway Postgres schemas for tests of the data layer.

    with scratch_schema() as conn:      # its own connection, search_path = a fresh schema
        store.upsert_prices(conn, ...)  # unqualified table names land in the scratch schema

migrations 001_init.sql and 010_ingest_*.sql are applied inside the schema,
and the whole schema is dropped on exit, so tests never touch real data.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

from src.db import MIGRATIONS_DIR, connect


def migration_files() -> list:
    return [MIGRATIONS_DIR / "001_init.sql", *sorted(MIGRATIONS_DIR.glob("010_ingest_*.sql"))]


@contextmanager
def scratch_schema() -> Iterator:
    conn = connect(autocommit=True)
    name = f"scratch_{uuid.uuid4().hex[:12]}"
    try:
        conn.execute(f'CREATE SCHEMA "{name}"')
        conn.execute(f'SET search_path TO "{name}"')
        for path in migration_files():
            conn.execute(path.read_text())
        yield conn
    finally:
        try:
            conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        finally:
            conn.close()
