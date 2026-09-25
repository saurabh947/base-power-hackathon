"""Redis layout helpers: fast command ids, the fleet layout, codecs. No services needed."""
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from src.config import load_config
from src.core.ids import make_command_id
from src.sim.fleet import generate_batteries
from src.sim.redis_layout import (
    CommandIds, FleetLayout, Keys, battery_id, layout_index, location_counts, pack, resp, to_utc_iso, unpack,
)

UTC = timezone.utc


def test_fast_command_ids_equal_make_command_id():
    rng = random.Random(3)
    for _ in range(20):
        run_id = uuid.UUID(int=rng.getrandbits(128)) if rng.random() < 0.5 else f"run-{rng.random()}"
        bids = [battery_id(rng.choice(["LZ_HOUSTON", "LZ_NORTH"]), rng.randrange(10**6)) for _ in range(50)]
        ts = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * rng.randrange(105120))
        factory = CommandIds(run_id)
        fast = [x.decode() for x in factory.generate(factory.prefixes(bids), ts).tolist()]
        assert fast == [make_command_id(run_id, b, ts) for b in bids]


def test_fast_command_ids_normalise_zones_and_accept_epoch_seconds():
    ts = datetime(2025, 11, 2, 6, 30, tzinfo=UTC)  # the fall-back hour in Chicago
    factory = CommandIds("r")
    pre = factory.prefixes(["LZ_NORTH-000001"])
    want = make_command_id("r", "LZ_NORTH-000001", ts)
    assert factory.generate(pre, ts.astimezone(ZoneInfo("America/Chicago")))[0].decode() == want
    assert factory.generate(pre, int(ts.timestamp()))[0].decode() == want
    assert factory.generate([], ts).shape == (0,)
    with pytest.raises(ValueError):
        factory.generate(pre, ts.replace(tzinfo=None))


def test_layout_matches_config_and_seeded_specs():
    cfg = load_config()
    layout = FleetLayout.build(cfg)
    assert layout.size == 20000 and layout.workers == 20
    assert layout.locations == ("LZ_HOUSTON", "LZ_NORTH") and layout.counts == (10000, 10000)
    assert layout.ids[0] == "LZ_HOUSTON-000000" and layout.ids[10000] == "LZ_NORTH-000000"
    assert layout.ids[-1] == "LZ_NORTH-009999" and len(set(layout.ids)) == 20000
    specs = generate_batteries(20000, cfg.battery, cfg.seed)
    assert np.array_equal(layout.capacity_kwh, [s.capacity_kwh for s in specs])
    assert np.array_equal(layout.power_kw, [s.power_kw for s in specs])
    owners = layout.default_owners()
    counts = np.bincount(owners, minlength=20)
    assert counts.tolist() == [1000] * 20
    for w in range(20):  # 500 Houston + 500 Dallas-Fort Worth each
        assert (layout.loc_of[owners == w] == 0).sum() == 500
    for g in (0, 1, 9999, 10000, 19999):
        assert layout.index_of(layout.ids[g]) == g
        assert layout_index(json.loads(json.dumps(layout.public())), layout.ids[g]) == (g, layout.ids[g].rsplit("-", 1)[0])
    for bad in ("LZ_HOUSTON-010000", "LZ_WEST-000001", "LZ_NORTH-12", "nope"):
        with pytest.raises(KeyError):
            layout.index_of(bad)
        with pytest.raises(KeyError):
            layout_index(layout.public(), bad)


def test_small_layouts_are_prefixes_of_the_full_fleet_specs():
    cfg = load_config()
    small = FleetLayout.build(cfg, size=101, workers=3)
    assert small.counts == (50, 51)
    assert np.array_equal(small.power_kw, FleetLayout.build(cfg).power_kw[:101])
    assert sorted(np.bincount(small.default_owners()).tolist()) == [33, 34, 34]
    assert location_counts(7, {"a": 0.5, "b": 0.5}) == [("a", 4), ("b", 3)]


def test_codecs_and_names():
    msg = {"t": "tick", "k": 3, "idx": np.arange(4, dtype=np.int32).tobytes(), "ids": ["a", "b"]}
    assert unpack(pack(msg)) == msg
    assert resp("HSET", "k", b"f", 1.5) == b"*4\r\n$4\r\nHSET\r\n$1\r\nk\r\n$1\r\nf\r\n$3\r\n1.5\r\n"
    k = Keys("t:")
    assert (k.commands(7), k.hb(7), k.lease(7), k.telemetry, k.dead_letter) == (
        "t:commands:7", "t:hb:7", "t:lease:7", "t:telemetry", "t:dead_letter")
    assert Keys().battery("LZ_NORTH-000001") == "battery:LZ_NORTH-000001"
    assert to_utc_iso(1747429500) == "2025-05-16T21:05:00Z"
