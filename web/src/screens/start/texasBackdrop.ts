// The Start page's faint Texas: where its city clusters go. Each metro gets a
// patch of dots on a hexagonal grid, biggest in the middle and fading out,
// like the live map's battery dots but drawn once and only for decoration.
// Positions use the map's own projection (src/map/outlinePath.ts), and every
// dot stays on land. Pure, so it is unit tested.

import type { City } from '../../lib/cities.ts';
import { pointInRing } from '../../map/geometry.ts';
import { lonLatToOutlineFrame, outlineFrameRing } from '../../map/outlinePath.ts';

export interface BackdropDot {
  x: number;
  y: number;
  r: number;
  opacity: number;
}

export interface BackdropCity {
  location: string;
  /** Centre, in the outline frame (km). */
  x: number;
  y: number;
  /** Radius of the soft glow behind the dots (km). */
  glow: number;
  dots: BackdropDot[];
}

/** Distance between neighbouring dots, km. */
export const DOT_SPACING_KM = 12;
/** A dot never grows past this share of the spacing, so dots never touch. */
const MAX_DOT_SHARE = 0.42;
/** Dots smaller than this are left out. */
const MIN_DOT_KM = 1.4;
/** How far a metro of 1,000 batteries spreads (Gaussian sigma, km); it grows with the square root of the count. */
const SIGMA_PER_1000_KM = 7.2;
/** How far out dots are drawn, in sigmas. */
const REACH_SIGMAS = 3;
const GLOW_SIGMAS = 3.4;

const ROW_KM = (DOT_SPACING_KM * Math.sqrt(3)) / 2;

interface Centre {
  location: string;
  x: number;
  y: number;
  sigma: number;
}

/** The clusters for `cities`, in the same order (cities without a usable centre or batteries are left out). */
export function backdropCities(cities: readonly City[]): BackdropCity[] {
  const centres: Centre[] = [];
  for (const c of cities) {
    const [lon, lat] = c.center;
    if (!(c.batteries > 0) || !Number.isFinite(lon) || !Number.isFinite(lat)) continue;
    const [x, y] = lonLatToOutlineFrame(lon, lat);
    centres.push({ location: c.location, x, y, sigma: SIGMA_PER_1000_KM * Math.sqrt(c.batteries / 1000) });
  }
  const ring = outlineFrameRing();

  // One shared hex grid, so neighbouring metros never draw overlapping dots: each cell goes to the city
  // that contributes most to it, and its size comes from all of them together.
  const cells = new Map<string, { x: number; y: number; density: number; owner: number; best: number }>();
  centres.forEach((c, k) => {
    const reach = REACH_SIGMAS * c.sigma;
    const j0 = Math.floor((c.y - reach) / ROW_KM);
    const j1 = Math.ceil((c.y + reach) / ROW_KM);
    for (let j = j0; j <= j1; j++) {
      const offset = (j & 1) * (DOT_SPACING_KM / 2);
      const i0 = Math.floor((c.x - reach - offset) / DOT_SPACING_KM);
      const i1 = Math.ceil((c.x + reach - offset) / DOT_SPACING_KM);
      for (let i = i0; i <= i1; i++) {
        const x = i * DOT_SPACING_KM + offset;
        const y = j * ROW_KM;
        const d2 = (x - c.x) ** 2 + (y - c.y) ** 2;
        if (d2 > reach * reach) continue;
        const w = Math.exp(-d2 / (2 * c.sigma * c.sigma));
        const key = `${i},${j}`;
        const cell = cells.get(key);
        if (!cell) cells.set(key, { x, y, density: w, owner: k, best: w });
        else {
          cell.density += w;
          if (w > cell.best) {
            cell.best = w;
            cell.owner = k;
          }
        }
      }
    }
  });

  const out: BackdropCity[] = centres.map((c) => ({ location: c.location, x: c.x, y: c.y, glow: GLOW_SIGMAS * c.sigma, dots: [] }));
  for (const cell of cells.values()) {
    const density = Math.min(1, cell.density);
    const r = MAX_DOT_SHARE * DOT_SPACING_KM * Math.sqrt(density);
    if (r < MIN_DOT_KM || !pointInRing(cell.x, cell.y, ring)) continue;
    out[cell.owner].dots.push({ x: cell.x, y: cell.y, r: Math.round(r * 100) / 100, opacity: Math.round((0.22 + 0.5 * density) * 100) / 100 });
  }
  // Top to bottom, left to right: a stable order whatever the order of the cities.
  for (const city of out) city.dots.sort((a, b) => a.y - b.y || a.x - b.x);
  return out;
}
