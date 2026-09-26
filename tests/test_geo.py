"""src.sim.geo: the fleet's cities and the simulated battery positions behind GET /fleet/layout.
No services needed."""
import subprocess
import sys

import numpy as np
import pytest

from src.config import REPO_ROOT, load_config
from src.sim import control as ctl
from src.sim import geo
from src.sim.fleet import location_counts

CFG = load_config()
COUNTS = list(location_counts(CFG.fleet.size, CFG.fleet.locations).items())


@pytest.fixture(scope="module")
def positions():
    return geo.fleet_positions(COUNTS, CFG.seed)


def blocks():
    first = 0
    for loc, n in COUNTS:
        yield loc, slice(first, first + n)
        first += n


def test_every_fleet_city_has_metadata_in_one_place():
    assert set(CFG.fleet.locations) <= set(geo.CITIES)
    assert {loc: (geo.city(loc).name, geo.city(loc).short) for loc in CFG.fleet.locations} == {
        "LZ_HOUSTON": ("Houston", "HOU"), "LZ_NORTH": ("Dallas–Fort Worth", "DFW"),
        "LZ_CPS": ("San Antonio", "SA"), "LZ_AEN": ("Austin", "AUS")}
    for loc in CFG.fleet.locations:  # event messages use the same names (src.sim.control.area)
        assert ctl.area(loc) == geo.city_name(loc) == geo.city(loc).name
    assert geo.city_name("LZ_WEST") == "LZ_WEST"  # no city: shown as its id
    with pytest.raises(KeyError):
        geo.city("LZ_WEST")


def test_sites_and_centres_are_real_places_inside_their_metro():
    lon_min, lat_min, lon_max, lat_max = geo.TEXAS_BBOX
    for c in geo.CITIES.values():
        assert lon_min < c.center_lonlat[0] < lon_max and lat_min < c.center_lonlat[1] < lat_max
        lon = np.array([s.lon for s in c.sites])
        lat = np.array([s.lat for s in c.sites])
        # every site sits well inside the metro radius (2 sigma of jitter to spare), weights are populations
        assert (geo.distance_km(c, lon, lat) + 2 * np.array([s.sigma_km for s in c.sites]) <= c.radius_km).all()
        assert all(s.weight > 0 and 1.0 <= s.sigma_km <= 8.0 for s in c.sites) and len(c.sites) >= 10
    centres = {loc: np.array(c.center_lonlat) for loc, c in geo.CITIES.items()}
    for a in geo.CITIES.values():  # the metros don't overlap, so a position tells the city
        for b in geo.CITIES.values():
            if a.location < b.location:
                d = geo.distance_km(a, *centres[b.location])
                assert d > a.radius_km + b.radius_km, (a.name, b.name)


def test_positions_are_inside_texas_and_their_metro(positions):
    assert positions.shape == (CFG.fleet.size, 2) and np.isfinite(positions).all()
    lon_min, lat_min, lon_max, lat_max = geo.TEXAS_BBOX
    assert (positions[:, 0] > lon_min).all() and (positions[:, 0] < lon_max).all()
    assert (positions[:, 1] > lat_min).all() and (positions[:, 1] < lat_max).all()
    for loc, sl in blocks():
        c = geo.city(loc)
        d = geo.distance_km(c, positions[sl, 0], positions[sl, 1])
        assert d.max() <= c.radius_km + 1e-9, loc
        assert np.median(d) < 0.5 * c.radius_km  # clustered on the metro, not smeared to its edge
        assert len(np.unique(np.round(positions[sl], 3), axis=0)) > 0.9 * (sl.stop - sl.start)  # spread out


def test_counts_per_city_follow_the_fleet_split(positions):
    for loc, sl in blocks():
        inside = {other: int((geo.distance_km(geo.city(other), positions[:, 0], positions[:, 1])
                              <= geo.city(other).radius_km).sum()) for other in CFG.fleet.locations}
        assert inside[loc] == sl.stop - sl.start == dict(COUNTS)[loc]


def test_sites_are_weighted_by_population(positions):
    """The biggest site of each metro gets about its population share of the batteries."""
    for loc, sl in blocks():
        c = geo.city(loc)
        big = max(c.sites, key=lambda s: s.weight)
        share = big.weight / sum(s.weight for s in c.sites)
        near = geo.distance_km(geo.City(loc, "", "", (big.lon, big.lat), 0.0, ()), positions[sl, 0], positions[sl, 1])
        assert 0.5 * share < np.mean(near <= 2.5 * big.sigma_km) < 2.0 * share + 0.1, loc


def test_positions_depend_only_on_seed_index_and_city(positions):
    again = geo.fleet_positions(COUNTS, CFG.seed)
    assert np.array_equal(again, positions)  # deterministic
    small = geo.fleet_positions(list(location_counts(2000, CFG.fleet.locations).items()), CFG.seed)
    houston = COUNTS[0][0]
    assert np.array_equal(small[:5], positions[:5])  # b00000.. keep their place at any fleet size
    # a battery's place depends on its own index and city only, never on its neighbours
    idx = np.array([17000, 3, 17599, 12345])
    one_by_one = np.array([geo.city_positions("LZ_CPS", [g], CFG.seed)[0] for g in idx])
    assert np.array_equal(geo.city_positions("LZ_CPS", idx, CFG.seed), one_by_one)
    assert not np.array_equal(geo.city_positions(houston, idx, CFG.seed), one_by_one)  # the city matters
    assert not np.array_equal(geo.fleet_positions(COUNTS[:1], CFG.seed + 1), positions[:COUNTS[0][1]])  # the seed too


def test_positions_are_stable_across_processes():
    code = ("from src.sim import geo; import hashlib; "
            "print(hashlib.sha256(geo.city_positions('LZ_AEN', range(17600, 20000), 42).tobytes()).hexdigest())")
    outs = {subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                           env={"PYTHONHASHSEED": str(seed), "PATH": ""}).stdout for seed in (1, 2)}
    assert len(outs) == 1 and len(outs.pop().strip()) == 64


def test_encoding_is_the_contracts_int16_grid(positions):
    raw = geo.encode_positions(positions)
    assert len(raw) == 4 * len(positions)
    v = np.frombuffer(raw, dtype="<i2").reshape(-1, 2)
    decoded = np.column_stack([-100 + v[:, 0] / 1000, 31 + v[:, 1] / 1000])  # docs/API.md
    assert np.abs(decoded - positions).max() <= 0.0005 + 1e-12  # about 50 m
    assert np.array_equal(geo.decode_positions(raw), decoded)
    with pytest.raises(ValueError):
        geo.quantize(np.array([[-140.0, 30.0]]))  # outside the Int16 range


def test_layout_version_hashes_size_shares_and_seed():
    v = geo.layout_version(CFG.fleet.size, CFG.fleet.locations, CFG.seed)
    assert v == geo.layout_version(CFG.fleet.size, dict(CFG.fleet.locations), CFG.seed)
    assert v != geo.layout_version(CFG.fleet.size - 1, CFG.fleet.locations, CFG.seed)
    assert v != geo.layout_version(CFG.fleet.size, CFG.fleet.locations, CFG.seed + 1)
    swapped = dict(reversed(list(CFG.fleet.locations.items())))
    assert v != geo.layout_version(CFG.fleet.size, swapped, CFG.seed)  # the order of the blocks matters


def test_city_phrases():
    four = list(CFG.fleet.locations)
    assert geo.cities_phrase(four, total=4) == "all four cities"
    assert geo.cities_phrase(["LZ_CPS", "LZ_AEN"], total=4) == "San Antonio and Austin"
    assert geo.cities_phrase(["LZ_NORTH"], total=4) == "Dallas–Fort Worth"
    assert geo.join_names(["Houston", "San Antonio", "Austin"]) == "Houston, San Antonio and Austin"
