"""PriceFeed over the committed ERCOT snapshot in data/snapshot (dev and tests, no database).

Behaves like the Postgres replay feed: native 15-min RT settlement prices,
forward-filled to 5-min ticks within their own interval, never across a gap.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from src.core.models import PriceTick
from src.feeds.memory import InMemoryPriceFeed

SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "data" / "snapshot"


def load_rt_rows(
    years: Iterable[int] = (2024, 2025),
    locations: Iterable[str] | None = ("LZ_HOUSTON", "LZ_NORTH"),
    snapshot_dir: Path = SNAPSHOT_DIR,
) -> list[PriceTick]:
    """Native 15-min rows (ts = interval start, UTC) from the snapshot files."""
    import pandas as pd

    frames = [
        pd.read_parquet(snapshot_dir / f"ercot_rt_{year}_hubs.parquet", columns=["ts", "location", "price_usd_mwh"])
        for year in years
    ]
    df = pd.concat(frames, ignore_index=True)
    if locations is not None:
        df = df[df["location"].isin(list(locations))]
    stamps = pd.to_datetime(df["ts"], utc=True)
    return [
        PriceTick(ts.to_pydatetime(), location, float(price), None, None)
        for ts, location, price in zip(stamps, df["location"], df["price_usd_mwh"])
    ]


def snapshot_feed(
    years: Iterable[int] = (2024, 2025), locations: Iterable[str] | None = ("LZ_HOUSTON", "LZ_NORTH")
) -> InMemoryPriceFeed:
    """PriceFeed over the snapshot, forward-filled to 5-min ticks."""
    return InMemoryPriceFeed.from_intervals(load_rt_rows(years, locations))
