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
from src.sim.fleet import assign_locations, generate_batteries, location_counts as split_counts
from src.sim.redis_layout import (
    CommandIds, FleetLayout, Keys, fleet_battery_id, layout_index, location_counts, pack, parse_battery_id, resp,
    to_utc_iso, unpack,
)

UTC = timezone.utc


def test_fast_command_ids_equal_make_command_id():
    rng = random.Random(3)
    for _ in range(20):
        run_id = uuid.UUID(int=rng.getrandbits(128)) if rng.random() < 0.5 else f"run-{rng.random()}"
        bids = [fleet_battery_id(rng.randrange(20000)) for _ in range(50)]
        ts = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * rng.randrange(105120))
        factory = CommandIds(run_id)
        fast = [x.decode() for x in factory.generate(factory.prefixes(bids), ts).tolist()]
        assert fast == [make_command_id(run_id, b, ts) for b in bids]


def test_fast_command_ids_normalise_zones_and_accept_epoch_seconds():
    ts = datetime(2025, 11, 2, 6, 30, tzinfo=UTC)  # the fall-back hour in Chicago
    factory = CommandIds("r")
    pre = factory.prefixes(["b10001"])
    want = make_command_id("r", "b10001", ts)
    assert factory.generate(pre, ts.astimezone(ZoneInfo("America/Chicago")))[0].decode() == want
    assert factory.generate(pre, int(ts.timestamp()))[0].decode() == want
    assert factory.generate([], ts).shape == (0,)
    with pytest.raises(ValueError):
        factory.generate(pre, ts.replace(tzinfo=None))


def test_layout_is_the_shared_fleet_split():
    cfg = load_config()
    layout = FleetLayout.build(cfg)
    n, workers = cfg.fleet.size, cfg.fleet.workers
    assert layout.size == n and layout.workers == workers
    counts = split_counts(n, cfg.fleet.locations)
    assert layout.locations == tuple(cfg.fleet.locations) and layout.counts == tuple(counts.values())
    specs = generate_batteries(n, cfg.battery, cfg.seed)
    assert list(layout.ids) == [s.battery_id for s in specs]  # b00000..b19999, as generate_batteries names them
    split = assign_locations(specs, cfg.fleet.locations)
    edges = []
    for li, (loc, block) in enumerate(split.items()):
        idx = layout.location_indices(li)
        assert [layout.ids[g] for g in idx] == [b.battery_id for b in block]
        assert layout.location_of(int(idx[0])) == layout.location_of(int(idx[-1])) == loc
        edges += [int(idx[0]), int(idx[-1])]
    assert np.array_equal(layout.capacity_kwh, [s.capacity_kwh for s in specs])
    assert np.array_equal(layout.power_kw, [s.power_kw for s in specs])
    # cohorts: contiguous blocks of the fleet in id order, so a controller serves one city (two at a boundary)
    owners = layout.default_owners()
    per = n // workers
    assert np.bincount(owners, minlength=workers).tolist() == [per] * workers
    assert np.array_equal(owners, np.repeat(np.arange(workers), per))
    for w in range(workers):
        mine = np.flatnonzero(owners == w)
        want = {loc: int(np.sum((mine >= layout.offsets[li]) & (mine < layout.offsets[li] + layout.counts[li])))
                for li, loc in enumerate(layout.locations)}
        assert layout.counts_by_location(mine) == {loc: c for loc, c in want.items() if c}
    spans = [w for w in range(workers) if len(layout.counts_by_location(np.flatnonzero(owners == w))) > 1]
    assert spans and len(spans) <= len(layout.locations) - 1  # at most one controller per city boundary
    public = json.loads(json.dumps(layout.public()))
    for g in (0, 1, *edges, n - 1):
        assert layout.index_of(layout.ids[g]) == g
        assert layout_index(public, layout.ids[g]) == (g, layout.location_of(g))
    for bad in ("b20000", "b0001", "b000001", "LZ_HOUSTON-000001", "B00001", "b-0001", "nope", ""):
        with pytest.raises(KeyError):
            layout.index_of(bad)
        with pytest.raises(KeyError):
            layout_index(public, bad)


def test_odd_sizes_split_exactly_like_src_sim_fleet():
    cfg = load_config()
    small = FleetLayout.build(cfg, size=101, workers=3)
    specs = generate_batteries(101, cfg.battery, cfg.seed)
    split = assign_locations(specs, cfg.fleet.locations)
    assert small.counts == tuple(len(b) for b in split.values())  # remainder by largest fraction, config order
    assert location_counts(101, cfg.fleet.locations) == [(loc, len(b)) for loc, b in split.items()]
    assert np.array_equal(small.power_kw, FleetLayout.build(cfg).power_kw[:101])  # a prefix of the full fleet
    assert sorted(np.bincount(small.default_owners()).tolist()) == [33, 34, 34]
    assert parse_battery_id("b00100", 101) == 100
    with pytest.raises(KeyError):
        parse_battery_id("b00101", 101)


def test_codecs_and_names():
    msg = {"t": "tick", "k": 3, "idx": np.arange(4, dtype=np.int32).tobytes(), "ids": ["a", "b"]}
    assert unpack(pack(msg)) == msg
    assert resp("HSET", "k", b"f", 1.5) == b"*4\r\n$4\r\nHSET\r\n$1\r\nk\r\n$1\r\nf\r\n$3\r\n1.5\r\n"
    k = Keys("t:")
    assert (k.commands(7), k.hb(7), k.lease(7), k.telemetry, k.dead_letter) == (
        "t:commands:7", "t:hb:7", "t:lease:7", "t:telemetry", "t:dead_letter")
    assert Keys().battery("b00001") == "battery:b00001"
    assert to_utc_iso(1747429500) == "2025-05-16T21:05:00Z"
