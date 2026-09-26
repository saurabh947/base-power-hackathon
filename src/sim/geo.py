"""The fleet's cities and simulated battery positions (docs/API.md `GET /fleet/layout`).

THE one place that maps a load zone to the city people see: `city_name("LZ_NORTH")` is
"Dallas–Fort Worth". Event messages, the Results text and the layout endpoint all read it
from here.

Positions are simulated, not addresses. Each metro is a hand-listed set of real city and
suburb centres (approximate lon/lat, weighted by rough population in thousands). Battery i
picks a centre with probability proportional to its weight, then moves by Gaussian jitter of
a few km (sigma_km per axis). A point that lands outside the metro radius is redrawn (up to
three times), and a last miss is pulled back towards its centre, so every battery sits within
`radius_km` of the metro centre.

Battery i's draws come from a counter-based hash of (seed, location, i, stream), not from a
sequential random stream, so its position depends only on (seed, i, its location): the same
across runs, processes and fleet sizes.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import numpy as np

GEO_VERSION = "geo-1"  # bump when the sites or the sampling change: it feeds the layout version

KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON_EQUATOR = 111.320

# docs/API.md: positions are Int16 pairs, lon = -100 + v/1000, lat = 31 + v/1000 (about 100 m)
LON_ORIGIN, LAT_ORIGIN, POSITION_SCALE = -100.0, 31.0, 1000.0

# Texas, generously: every simulated position must fall inside this box (tests).
TEXAS_BBOX = (-106.65, 25.84, -93.51, 36.50)  # lon_min, lat_min, lon_max, lat_max


@dataclass(frozen=True)
class Site:
    """A real city or suburb centre inside a metro (approximate)."""

    name: str
    lon: float
    lat: float
    weight: float  # rough population, thousands
    sigma_km: float  # Gaussian jitter per axis


@dataclass(frozen=True)
class City:
    location: str  # ERCOT load zone the batteries settle in
    name: str  # what people see
    short: str
    center_lonlat: tuple[float, float]
    radius_km: float  # every battery of this city lies within this distance of the centre
    sites: tuple[Site, ...]


def _sites(*rows: tuple[str, float, float, float, float]) -> tuple[Site, ...]:
    return tuple(Site(*row) for row in rows)


_CITY_LIST = (
    City("LZ_HOUSTON", "Houston", "HOU", (-95.37, 29.76), 70.0, _sites(
        ("Houston, downtown and inner loop", -95.3698, 29.7604, 900, 6.0),
        ("Houston, Westchase and Alief", -95.5600, 29.7200, 500, 5.0),
        ("Houston, Aldine and Greenspoint", -95.4000, 29.9300, 400, 5.0),
        ("Houston, Hobby and South Belt", -95.2700, 29.6600, 300, 4.0),
        ("Pasadena", -95.2091, 29.6911, 150, 3.0),
        ("Pearland", -95.2860, 29.5636, 125, 3.0),
        ("League City", -95.0949, 29.5075, 115, 3.0),
        ("Sugar Land", -95.6349, 29.6197, 110, 3.0),
        ("The Woodlands", -95.4613, 30.1658, 115, 3.5),
        ("Conroe", -95.4561, 30.3119, 95, 3.0),
        ("Baytown", -94.9774, 29.7355, 85, 2.5),
        ("Missouri City", -95.5377, 29.6186, 75, 3.0),
        ("Katy and Cinco Ranch", -95.8245, 29.7858, 150, 4.0),
        ("Cypress", -95.6972, 29.9691, 200, 4.5),
        ("Spring", -95.4172, 30.0799, 120, 4.0),
        ("Atascocita and Humble", -95.1766, 29.9988, 100, 3.5),
        ("Kingwood", -95.1872, 30.0496, 80, 3.0),
        ("Friendswood", -95.2010, 29.5294, 41, 2.5),
        ("Richmond and Rosenberg", -95.7608, 29.5822, 60, 3.0),
        ("Tomball", -95.6161, 30.0972, 25, 3.0),
        ("Channelview", -95.1146, 29.7760, 45, 2.5),
    )),
    City("LZ_NORTH", "Dallas–Fort Worth", "DFW", (-96.97, 32.82), 65.0, _sites(
        ("Dallas, downtown and central", -96.7970, 32.7767, 800, 6.0),
        ("Dallas, North Dallas", -96.8000, 32.9200, 350, 4.0),
        ("Dallas, Oak Cliff", -96.8300, 32.7000, 250, 4.0),
        ("Fort Worth, central", -97.3308, 32.7555, 600, 6.0),
        ("Fort Worth, north", -97.3200, 32.9000, 250, 4.5),
        ("Arlington", -97.1081, 32.7357, 395, 4.0),
        ("Plano", -96.6989, 33.0198, 285, 3.5),
        ("Garland", -96.6389, 32.9126, 245, 3.0),
        ("Irving", -96.9489, 32.8140, 255, 3.5),
        ("Frisco", -96.8236, 33.1507, 200, 3.5),
        ("McKinney", -96.6398, 33.1972, 195, 3.5),
        ("Grand Prairie", -96.9978, 32.7460, 195, 3.5),
        ("Denton", -97.1331, 33.2148, 140, 3.5),
        ("Mesquite", -96.5992, 32.7668, 150, 3.0),
        ("Carrollton", -96.8900, 32.9756, 135, 3.0),
        ("Richardson", -96.7299, 32.9483, 120, 2.5),
        ("Lewisville", -96.9942, 33.0462, 110, 3.0),
        ("Allen", -96.6706, 33.1032, 105, 2.5),
        ("Flower Mound", -97.0970, 33.0146, 75, 3.0),
        ("Mansfield", -97.1417, 32.5632, 73, 3.0),
        ("North Richland Hills", -97.2289, 32.8343, 70, 2.5),
        ("Rowlett", -96.5639, 32.9029, 63, 2.5),
        ("DeSoto", -96.8570, 32.5899, 56, 2.5),
        ("Cedar Hill", -96.9561, 32.5885, 49, 2.5),
        ("Wylie", -96.5389, 33.0151, 57, 2.5),
        ("Burleson", -97.3208, 32.5421, 50, 2.5),
        ("Rockwall", -96.4597, 32.9312, 48, 2.5),
        ("Prosper and Celina", -96.8000, 33.2400, 60, 3.0),
    )),
    City("LZ_CPS", "San Antonio", "SA", (-98.49, 29.42), 55.0, _sites(
        ("San Antonio, downtown and central", -98.4936, 29.4241, 500, 5.0),
        ("San Antonio, Stone Oak and North Central", -98.4800, 29.6000, 250, 4.5),
        ("San Antonio, Medical Center", -98.5800, 29.5100, 250, 4.5),
        ("San Antonio, West Side", -98.6600, 29.4300, 180, 4.5),
        ("San Antonio, South Side", -98.4900, 29.3300, 160, 4.5),
        ("San Antonio, East Side", -98.3900, 29.4600, 120, 4.0),
        ("Converse", -98.3161, 29.5180, 28, 2.5),
        ("Schertz", -98.2697, 29.5522, 42, 2.5),
        ("Universal City and Live Oak", -98.3000, 29.5600, 35, 2.5),
        ("Helotes", -98.6897, 29.5780, 20, 3.0),
        ("New Braunfels", -98.1245, 29.7030, 90, 3.5),
        ("Boerne", -98.7320, 29.7947, 20, 3.0),
    )),
    City("LZ_AEN", "Austin", "AUS", (-97.74, 30.27), 55.0, _sites(
        ("Austin, downtown and central", -97.7431, 30.2672, 450, 5.0),
        ("Austin, North Austin", -97.6900, 30.4000, 250, 4.5),
        ("Austin, South Austin", -97.7900, 30.1800, 220, 4.5),
        ("Austin, East Austin and Del Valle", -97.6600, 30.2500, 90, 4.0),
        ("Austin, Four Points", -97.8200, 30.4300, 90, 4.0),
        ("Round Rock", -97.6789, 30.5083, 120, 3.5),
        ("Cedar Park", -97.8203, 30.5052, 77, 3.0),
        ("Pflugerville", -97.6200, 30.4394, 65, 3.0),
        ("Georgetown", -97.6770, 30.6333, 67, 3.5),
        ("Leander", -97.8531, 30.5788, 60, 3.0),
        ("San Marcos", -97.9414, 29.8833, 67, 3.0),
        ("Kyle", -97.8772, 29.9891, 45, 3.0),
        ("Buda", -97.8403, 30.0852, 13, 2.5),
        ("Lakeway", -97.9795, 30.3635, 19, 2.5),
        ("Hutto", -97.5467, 30.5427, 27, 2.5),
        ("Manor", -97.5569, 30.3405, 13, 2.5),
    )),
)
CITIES: dict[str, City] = {c.location: c for c in _CITY_LIST}


def city(location: str) -> City:
    """The city of a load zone. KeyError for a location with no city."""
    return CITIES[location]


def city_name(location: str) -> str:
    """'Dallas–Fort Worth' for LZ_NORTH; an unknown location is shown as its id."""
    c = CITIES.get(location)
    return c.name if c else location


def join_names(names: Sequence[str]) -> str:
    """'Houston', 'Houston and Austin', 'Houston, San Antonio and Austin'."""
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def cities_phrase(locations: Sequence[str], total: int | None = None) -> str:
    """How messages name a group of cities: 'all four cities' when it is the whole fleet, else the names."""
    locations = list(locations)
    if total is not None and len(locations) == total and total > 1:
        return "both cities" if total == 2 else f"all {_NUMBER_WORDS.get(total, str(total))} cities"
    return join_names([city_name(loc) for loc in locations])


_NUMBER_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}


# --- positions ----------------------------------------------------------------------------------

_U64 = np.uint64
_GOLDEN = _U64(0x9E3779B97F4A7C15)


def _mix(x: np.ndarray) -> np.ndarray:
    """SplitMix64 finaliser over uint64 (wrapping arithmetic)."""
    with np.errstate(over="ignore"):
        x = (x + _GOLDEN).astype(np.uint64)
        x = (x ^ (x >> _U64(30))) * _U64(0xBF58476D1CE4E5B9)
        x = (x ^ (x >> _U64(27))) * _U64(0x94D049BB133111EB)
        return x ^ (x >> _U64(31))


def _key(seed: int, location: str) -> np.uint64:
    digest = hashlib.sha256(f"fleet-sim/{GEO_VERSION}/{int(seed)}/{location}".encode()).digest()
    return _U64(int.from_bytes(digest[:8], "little"))


def uniforms(seed: int, location: str, idx: np.ndarray, stream: int) -> np.ndarray:
    """U[0, 1) per battery index: a pure function of (seed, location, index, stream)."""
    idx = np.asarray(idx, dtype=np.uint64)
    with np.errstate(over="ignore"):
        h = _mix(_mix(idx ^ _key(seed, location)) + _U64(stream) * _GOLDEN)
    return (h >> _U64(11)).astype(np.float64) * (1.0 / 9007199254740992.0)  # 53 random bits


def _normals(seed: int, location: str, idx: np.ndarray, stream: int) -> tuple[np.ndarray, np.ndarray]:
    """Two independent N(0, 1) per battery (Box-Muller) from streams `stream` and `stream + 1`."""
    u1 = uniforms(seed, location, idx, stream)
    u2 = uniforms(seed, location, idx, stream + 1)
    r = np.sqrt(-2.0 * np.log1p(-u1))  # 1 - u1 is in (0, 1]
    return r * np.cos(2.0 * math.pi * u2), r * np.sin(2.0 * math.pi * u2)


def km_per_deg_lon(lat: float | np.ndarray) -> float | np.ndarray:
    return KM_PER_DEG_LON_EQUATOR * np.cos(np.radians(lat))


def distance_km(c: City, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Distance from the city centre (local equirectangular: fine at metro scale)."""
    lon0, lat0 = c.center_lonlat
    dx = (np.asarray(lon) - lon0) * km_per_deg_lon(lat0)
    dy = (np.asarray(lat) - lat0) * KM_PER_DEG_LAT
    return np.hypot(dx, dy)


RETRIES = 3


def city_positions(location: str, idx: Iterable[int] | np.ndarray, seed: int) -> np.ndarray:
    """(n, 2) float64 [lon, lat] for the batteries with these global indices in `location`."""
    c = city(location)
    idx = np.asarray(idx, dtype=np.int64)
    if idx.size and idx.min() < 0:
        raise ValueError("battery indices must be >= 0")
    weights = np.array([s.weight for s in c.sites], dtype=np.float64)
    cum = np.cumsum(weights) / weights.sum()
    pick = np.minimum(np.searchsorted(cum, uniforms(seed, location, idx, 0), side="right"), len(c.sites) - 1)
    site_lon = np.array([s.lon for s in c.sites])[pick]
    site_lat = np.array([s.lat for s in c.sites])[pick]
    sigma = np.array([s.sigma_km for s in c.sites])[pick]
    lon = np.empty(len(idx))
    lat = np.empty(len(idx))
    todo = np.ones(len(idx), dtype=bool)
    for attempt in range(RETRIES + 1):
        if not todo.any():
            break
        zx, zy = _normals(seed, location, idx[todo], 1 + 2 * attempt)
        la = site_lat[todo] + sigma[todo] * zy / KM_PER_DEG_LAT
        lo = site_lon[todo] + sigma[todo] * zx / km_per_deg_lon(site_lat[todo])
        if attempt == RETRIES:  # last resort: pull the jitter back towards its site until inside
            for _ in range(30):
                out = distance_km(c, lo, la) > c.radius_km
                if not out.any():
                    break
                lo = np.where(out, site_lon[todo] + (lo - site_lon[todo]) * 0.5, lo)
                la = np.where(out, site_lat[todo] + (la - site_lat[todo]) * 0.5, la)
        lon[todo], lat[todo] = lo, la
        todo[todo] = distance_km(c, lo, la) > c.radius_km
    return np.column_stack([lon, lat])


def fleet_positions(counts: Sequence[tuple[str, int]], seed: int) -> np.ndarray:
    """(N, 2) [lon, lat] for a fleet split into contiguous blocks in this order (src.sim.fleet)."""
    parts, first = [], 0
    for location, n in counts:
        parts.append(city_positions(location, np.arange(first, first + int(n)), seed))
        first += int(n)
    return np.concatenate(parts) if parts else np.empty((0, 2))


def quantize(lonlat: np.ndarray) -> np.ndarray:
    """(N, 2) int16 as docs/API.md encodes them: lon = -100 + v/1000, lat = 31 + v/1000."""
    lonlat = np.asarray(lonlat, dtype=np.float64)
    v = np.rint((lonlat - np.array([LON_ORIGIN, LAT_ORIGIN])) * POSITION_SCALE)
    if v.size and (v.min() < -32768 or v.max() > 32767):
        raise ValueError("position outside the Int16 range of the layout encoding")
    return v.astype(np.int16)


def dequantize(v: np.ndarray) -> np.ndarray:
    """int16 pairs back to [lon, lat] (what the map draws)."""
    return np.asarray(v, dtype=np.float64) / POSITION_SCALE + np.array([LON_ORIGIN, LAT_ORIGIN])


def encode_positions(lonlat: np.ndarray) -> bytes:
    """Little-endian Int16 [lon0, lat0, lon1, lat1, ...]."""
    return quantize(lonlat).astype("<i2").tobytes()


def decode_positions(data: bytes) -> np.ndarray:
    return dequantize(np.frombuffer(data, dtype="<i2").reshape(-1, 2))


def layout_version(size: int, shares: Mapping[str, float], seed: int) -> str:
    """A hash over the fleet size, the location shares (in order) and the seed (plus GEO_VERSION)."""
    text = repr((int(size), [(str(k), float(v)) for k, v in shares.items()], int(seed), GEO_VERSION))
    return hashlib.sha256(text.encode()).hexdigest()[:16]
