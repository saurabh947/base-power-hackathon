"""Deterministic command ids (RUNLOG: uuid5 over run, battery and tick).

A replayed command gets the same id as the original, so workers can dedupe it
without a global seen-set.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

# uuid5(NAMESPACE_URL, "fleet-sim/command-id"). Never change it: every
# command_id would change and replays of an in-flight run would stop deduping.
COMMAND_NAMESPACE = uuid.UUID("a0f87879-6af1-5636-a60b-4eaf88d49245")


def make_command_id(run_id: str | uuid.UUID, battery_id: str, tick_ts: datetime) -> str:
    """uuid5(NAMESPACE, f"{run_id}|{battery_id}|{tick_ts.isoformat()}") with tick_ts in UTC.

    The tick is normalised to UTC first, so the same instant expressed in any
    zone gives the same id. Naive datetimes raise ValueError.
    """
    if tick_ts.tzinfo is None or tick_ts.utcoffset() is None:
        raise ValueError(f"tick_ts must be timezone-aware, got {tick_ts!r}")
    ts = tick_ts.astimezone(timezone.utc).isoformat()
    return str(uuid.uuid5(COMMAND_NAMESPACE, f"{run_id}|{battery_id}|{ts}"))
