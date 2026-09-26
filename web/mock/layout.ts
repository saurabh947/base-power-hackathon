// SIMULATED battery positions for the mock's GET /fleet/layout (dev and tests
// only; never shipped). FLEET_SIM_MOCK_DO_NOT_SHIP
//
// Each metro is a seeded mixture of Gaussian blobs around real places (downtown,
// suburbs, satellite cities), so the dots look like a metro area. No real
// addresses: positions are rounded to 0.001° (about 100 m), as the API sends them.

import type { FleetLayout, LayoutCity } from '../src/api/types.ts';
import { DEFAULT_FLEET_SIZE, DEFAULT_SHARES, batteryRanges } from '../src/lib/fleet.ts';
import { projectLonLat } from '../src/map/albers.ts';
import { pointInRing } from '../src/map/geometry.ts';
import { TEXAS_OUTLINE } from '../src/map/texasOutline.ts';

interface Blob {
  lon: number;
  lat: number;
  weight: number;
  /** Spread, km (one standard deviation). */
  km: number;
}

interface Metro {
  location: string;
  name: string;
  short: string;
  center: [number, number];
  blobs: Blob[];
}

const b = (lon: number, lat: number, weight: number, km: number): Blob => ({ lon, lat, weight, km });

/** In config order (the battery id blocks run in this order). */
export const METROS: Metro[] = [
  {
    location: 'LZ_HOUSTON',
    name: 'Houston',
    short: 'HOU',
    center: [-95.37, 29.76],
    blobs: [
      b(-95.37, 29.76, 0.17, 8), // downtown and the inner loop
      b(-95.82, 29.79, 0.1, 7), // Katy
      b(-95.63, 29.6, 0.1, 6), // Sugar Land, Missouri City
      b(-95.47, 30.15, 0.11, 8), // The Woodlands, Spring
      b(-95.68, 29.97, 0.1, 7), // Cypress
      b(-95.28, 29.56, 0.08, 6), // Pearland, Friendswood
      b(-95.08, 29.7, 0.07, 7), // Pasadena, Baytown
      b(-95.12, 29.5, 0.06, 5), // League City, Clear Lake
      b(-95.2, 30.0, 0.08, 7), // Humble, Kingwood
      b(-95.46, 30.31, 0.04, 6), // Conroe
      b(-95.77, 29.56, 0.05, 5), // Richmond, Rosenberg
      b(-95.5, 29.88, 0.04, 9), // spread across the metro
    ],
  },
  {
    location: 'LZ_NORTH',
    name: 'Dallas–Fort Worth',
    short: 'DFW',
    center: [-96.97, 32.82],
    blobs: [
      b(-96.8, 32.79, 0.13, 9), // Dallas
      b(-97.33, 32.75, 0.12, 9), // Fort Worth
      b(-97.1, 32.71, 0.1, 7), // Arlington, Grand Prairie
      b(-96.72, 33.02, 0.1, 7), // Plano, Richardson
      b(-96.8, 33.17, 0.1, 8), // Frisco, McKinney
      b(-96.95, 32.87, 0.07, 6), // Irving, Las Colinas
      b(-96.6, 32.85, 0.08, 7), // Garland, Mesquite, Rowlett
      b(-97.05, 33.12, 0.08, 8), // Denton, Lewisville
      b(-97.0, 32.58, 0.07, 8), // Mansfield, Cedar Hill, DeSoto
      b(-97.22, 32.9, 0.07, 6), // Keller, North Richland Hills
      b(-96.6, 33.08, 0.05, 6), // Allen, Wylie
      b(-96.95, 32.85, 0.03, 14), // spread across the metro
    ],
  },
  {
    location: 'LZ_CPS',
    name: 'San Antonio',
    short: 'SA',
    center: [-98.49, 29.42],
    blobs: [
      b(-98.49, 29.43, 0.25, 7), // downtown
      b(-98.48, 29.6, 0.2, 6), // North Central, Stone Oak
      b(-98.7, 29.47, 0.15, 7), // west side, Alamo Ranch
      b(-98.3, 29.55, 0.15, 6), // Schertz, Converse
      b(-98.52, 29.3, 0.1, 7), // south side
      b(-98.12, 29.7, 0.08, 5), // New Braunfels
      b(-98.68, 29.7, 0.07, 6), // Boerne, Helotes
    ],
  },
  {
    location: 'LZ_AEN',
    name: 'Austin',
    short: 'AUS',
    center: [-97.74, 30.27],
    blobs: [
      b(-97.74, 30.28, 0.26, 6), // central Austin
      b(-97.68, 30.49, 0.18, 6), // north Austin, Round Rock
      b(-97.83, 30.55, 0.12, 6), // Cedar Park, Leander
      b(-97.6, 30.43, 0.1, 5), // Pflugerville, Manor
      b(-97.82, 30.12, 0.14, 6), // south Austin, Buda
      b(-97.68, 30.64, 0.08, 5), // Georgetown
      b(-97.95, 30.34, 0.07, 5), // Westlake, Lakeway
      b(-97.86, 30.0, 0.05, 5), // Kyle
    ],
  },
];

const KM_PER_DEG_LAT = 111.2;

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function gaussian(rand: () => number): number {
  // Box–Muller, truncated at 2.5 standard deviations.
  for (;;) {
    const u = Math.max(1e-12, rand());
    const v = rand();
    const z = Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
    if (Math.abs(z) <= 2.5) return z;
  }
}

export interface MockLayout {
  json: FleetLayout;
  /** Rounded [lon, lat] of every battery, as the API sends them. */
  lonlat: Float64Array;
}

/** The seeded positions: the same fleet always gets the same map. */
export function buildMockLayout(size = DEFAULT_FLEET_SIZE, shares = DEFAULT_SHARES, seed = 20260926): MockLayout {
  const ranges = batteryRanges(size, shares);
  const rand = mulberry32(seed);
  const bytes = new Uint8Array(size * 4);
  const view = new DataView(bytes.buffer);
  const lonlat = new Float64Array(size * 2);
  const cities: LayoutCity[] = [];
  for (const range of ranges) {
    const metro = METROS.find((m) => m.location === range.location);
    if (!metro) continue;
    const total = metro.blobs.reduce((s, x) => s + x.weight, 0);
    for (let g = range.first; g <= range.last; g++) {
      let pick = rand() * total;
      let blob = metro.blobs[0];
      for (const x of metro.blobs) {
        pick -= x.weight;
        if (pick <= 0) {
          blob = x;
          break;
        }
      }
      // On land: resample anything that falls outside the Texas outline (Galveston Bay, for one).
      let vLon = 0;
      let vLat = 0;
      for (let attempt = 0; attempt < 50; attempt++) {
        const lat = blob.lat + (gaussian(rand) * blob.km) / KM_PER_DEG_LAT;
        const lon = blob.lon + (gaussian(rand) * blob.km) / (KM_PER_DEG_LAT * Math.cos((blob.lat * Math.PI) / 180));
        vLon = Math.round((lon + 100) * 1000);
        vLat = Math.round((lat - 31) * 1000);
        if (pointInRing(...projectLonLat(-100 + vLon / 1000, 31 + vLat / 1000), TEXAS_OUTLINE)) break;
      }
      view.setInt16(4 * g, vLon, true);
      view.setInt16(4 * g + 2, vLat, true);
      lonlat[2 * g] = -100 + vLon / 1000;
      lonlat[2 * g + 1] = 31 + vLat / 1000;
    }
    cities.push({
      location: metro.location,
      name: metro.name,
      short: metro.short,
      batteries: range.last - range.first + 1,
      first_battery: range.first,
      center_lonlat: metro.center,
    });
  }
  return {
    json: {
      version: `mock-layout-1-${size}-${seed}`,
      fleet_size: size,
      cities,
      positions: Buffer.from(bytes).toString('base64'),
    },
    lonlat,
  };
}
