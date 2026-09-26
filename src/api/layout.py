"""GET /fleet/layout (docs/API.md Layout): where every battery sits, for the live map.

Static per fleet, so it is built once per (size, shares, seed) and cached; `version` is a hash over
those (and src.sim.geo.GEO_VERSION), so clients can cache it by version.
"""
from __future__ import annotations

import base64
from functools import lru_cache
from typing import Mapping

from src.sim import geo
from src.sim.fleet import location_counts


def layout_message(size: int, shares: Mapping[str, float], seed: int) -> dict:
    """The Layout object for a fleet of `size` split by `shares` (config order) and seeded by `seed`."""
    return _cached(int(size), tuple((str(k), float(v)) for k, v in shares.items()), int(seed))


@lru_cache(maxsize=8)
def _cached(size: int, shares: tuple[tuple[str, float], ...], seed: int) -> dict:
    counts = list(location_counts(size, dict(shares)).items())
    lonlat = geo.fleet_positions(counts, seed)
    cities, first = [], 0
    for location, n in counts:
        c = geo.city(location)
        cities.append({"location": location, "name": c.name, "short": c.short, "batteries": int(n),
                       "first_battery": first, "center_lonlat": [c.center_lonlat[0], c.center_lonlat[1]]})
        first += int(n)
    return {
        "version": geo.layout_version(size, dict(shares), seed),
        "fleet_size": size,
        "cities": cities,
        "positions": base64.b64encode(geo.encode_positions(lonlat)).decode("ascii"),
    }


def battery_lonlat(g: int, location: str, seed: int) -> list[float]:
    """[lon, lat] of battery g exactly as the map draws it: the layout's Int16 grid, 3 decimals.

    A position depends only on (seed, g, location), so no fleet size is needed.
    """
    v = geo.quantize(geo.city_positions(location, [int(g)], int(seed)))
    lon, lat = geo.dequantize(v)[0]
    return [round(float(lon), 3), round(float(lat), 3)]
