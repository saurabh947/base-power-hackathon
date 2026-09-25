"""Postgres connection and migrations.

DATABASE_URL wins (local dev: scripts/dev/services.sh prints it). Otherwise the
Cloud SQL form is used: DB_HOST=/cloudsql/<connection name>, DB_NAME, DB_USER,
DB_PASSWORD (from Secret Manager). Run `python -m src.db migrate` to apply
migrations/*.sql in name order, each exactly once.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("DB_HOST")
    if not host:
        raise RuntimeError("set DATABASE_URL (local) or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD (Cloud SQL)")
    from psycopg.conninfo import make_conninfo

    return make_conninfo(
        host=host,
        dbname=os.environ.get("DB_NAME", "fleetsim"),
        user=os.environ.get("DB_USER", "fleetsim"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def connect(**kwargs):
    import psycopg

    return psycopg.connect(dsn(), **kwargs)


def migrate() -> list[str]:
    """Apply pending migrations; return the names applied by this call."""
    applied = []
    with connect(autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    if sys.argv[1:] != ["migrate"]:
        sys.exit("usage: python -m src.db migrate")
    print("applied:", ", ".join(migrate()) or "nothing new")
