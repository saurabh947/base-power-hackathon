// GET /fleet/layout (docs/API.md): where every battery sits. Read defensively
// and projected once per layout version with the same EPSG:3083 function as the
// Texas outline.
//
// positions: base64 of a little-endian Int16Array [lon0, lat0, lon1, lat1, …],
// one pair per battery in id order, lon = -100 + v/1000 and lat = 31 + v/1000.

import { DEFAULT_CITIES, type City } from '../lib/cities.ts';
import { projectLonLat } from './albers.ts';

export interface MapLayout {
  version: string;
  size: number;
  /** In config (battery id) order. */
  cities: City[];
  lon: Float32Array;
  lat: Float32Array;
  /** EPSG:3083 metres. */
  x: Float64Array;
  y: Float64Array;
  /** Index into `cities` for every battery (NO_CITY when a battery is in no block). */
  cityOf: Uint8Array;
}

export const NO_CITY = 255;

export function base64ToBytes(b64: string): Uint8Array | null {
  let binary: string;
  try {
    binary = atob(b64.replace(/\s+/g, ''));
  } catch {
    return null;
  }
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** [lon0, lat0, lon1, lat1, …] in degrees from the positions field; null if it is not `size` pairs. */
export function decodePositions(b64: string, size: number): Float64Array | null {
  const bytes = base64ToBytes(b64);
  if (!bytes || bytes.length < size * 4) return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const out = new Float64Array(size * 2);
  for (let i = 0; i < size; i++) {
    out[2 * i] = -100 + view.getInt16(4 * i, true) / 1000;
    out[2 * i + 1] = 31 + view.getInt16(4 * i + 2, true) / 1000;
  }
  return out;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function readCity(v: unknown): City | null {
  if (!isObject(v) || typeof v.location !== 'string') return null;
  const fallback = DEFAULT_CITIES.find((c) => c.location === v.location);
  const batteries = Number(v.batteries);
  const first = Number(v.first_battery);
  const center = Array.isArray(v.center_lonlat) ? v.center_lonlat.map(Number) : [];
  if (!Number.isInteger(batteries) || batteries < 0 || !Number.isInteger(first) || first < 0) return null;
  return {
    location: v.location,
    name: typeof v.name === 'string' && v.name ? v.name : (fallback?.name ?? v.location),
    short: typeof v.short === 'string' && v.short ? v.short : (fallback?.short ?? v.location),
    batteries,
    firstBattery: first,
    center:
      center.length === 2 && center.every(Number.isFinite)
        ? [center[0], center[1]]
        : (fallback?.center ?? [Number.NaN, Number.NaN]),
  };
}

/** A usable layout from the raw JSON, or null. */
export function readLayout(raw: unknown): MapLayout | null {
  if (!isObject(raw) || typeof raw.positions !== 'string') return null;
  const size = Number(raw.fleet_size);
  if (!Number.isInteger(size) || size <= 0 || size > 1_000_000) return null;
  const lonlat = decodePositions(raw.positions, size);
  if (!lonlat) return null;
  const cities = (Array.isArray(raw.cities) ? raw.cities : [])
    .map(readCity)
    .filter((c): c is City => c !== null)
    .toSorted((a, b) => a.firstBattery - b.firstBattery)
    .slice(0, NO_CITY);

  const lon = new Float32Array(size);
  const lat = new Float32Array(size);
  const x = new Float64Array(size);
  const y = new Float64Array(size);
  const cityOf = new Uint8Array(size).fill(NO_CITY);
  for (let i = 0; i < size; i++) {
    lon[i] = lonlat[2 * i];
    lat[i] = lonlat[2 * i + 1];
    const [px, py] = projectLonLat(lonlat[2 * i], lonlat[2 * i + 1]);
    x[i] = px;
    y[i] = py;
  }
  cities.forEach((c, k) => {
    const end = Math.min(size, c.firstBattery + c.batteries);
    for (let i = c.firstBattery; i < end; i++) cityOf[i] = k;
  });
  return { version: typeof raw.version === 'string' ? raw.version : '', size, cities, lon, lat, x, y, cityOf };
}
