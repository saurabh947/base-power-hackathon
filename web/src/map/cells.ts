// Screen-space aggregation for the battery dots. Batteries are binned into a
// hexagonal grid in CSS pixels (a few hundred to a few thousand cells, whatever
// the screen size); each non-empty cell becomes one dot whose AREA is
// proportional to its battery count, so 20,000 batteries stay readable.
//
// Rebuilt only when the layout or the viewport changes. Per frame, only the
// status codes are re-counted per cell (aggregateCodes).

import { NO_CITY } from './layout.ts';
import { CODES } from './status.ts';

export interface Cells {
  count: number;
  /** Centre-to-centre distance of the hex grid, CSS px. */
  spacing: number;
  /** Where each dot is drawn, CSS px. */
  x: Float32Array;
  y: Float32Array;
  /** Batteries in each cell. */
  n: Uint32Array;
  /** The city most of the cell's batteries belong to (NO_CITY if none). */
  city: Uint8Array;
  /** The cell of every battery (-1 if it has none). */
  cellOf: Int32Array;
  /** The largest cell count, and the 95th percentile (the size reference for dots). */
  maxN: number;
  refN: number;
}

const SQRT3 = Math.sqrt(3);

/** Axial coordinates [q, r] of the pointy-top hex (centre spacing `spacing`) that contains (x, y). */
export function hexOf(x: number, y: number, spacing: number): [number, number] {
  const size = spacing / SQRT3;
  const q = ((SQRT3 / 3) * x - y / 3) / size;
  const r = ((2 / 3) * y) / size;
  const s = -q - r;
  let rq = Math.round(q);
  let rr = Math.round(r);
  const rs = Math.round(s);
  const dq = Math.abs(rq - q);
  const dr = Math.abs(rr - r);
  const ds = Math.abs(rs - s);
  if (dq > dr && dq > ds) rq = -rr - rs;
  else if (dr > ds) rr = -rq - rs;
  return [rq, rr];
}

/** The centre of hex [q, r]. */
export function hexCenter(q: number, r: number, spacing: number): [number, number] {
  const size = spacing / SQRT3;
  return [size * SQRT3 * (q + r / 2), size * 1.5 * r];
}

/**
 * Bin screen positions `sx`/`sy` (NaN = not placed) into hex cells. Each dot sits between the
 * centroid of its batteries and its hex centre (`snap` 0 = centroid, 1 = centre): organic, but tidy.
 */
export function buildCells(
  sx: ArrayLike<number>,
  sy: ArrayLike<number>,
  cityOf: Uint8Array,
  spacing: number,
  cityCount: number,
  snap = 0.5,
): Cells {
  const size = sx.length;
  const cellOf = new Int32Array(size).fill(-1);
  const keys = new Map<number, number>();
  const hexQ: number[] = [];
  const hexR: number[] = [];
  const sumX: number[] = [];
  const sumY: number[] = [];
  const counts: number[] = [];
  for (let i = 0; i < size; i++) {
    const x = sx[i];
    const y = sy[i];
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    const [q, r] = hexOf(x, y, spacing);
    const key = (q + 32768) * 65536 + (r + 32768);
    let cell = keys.get(key);
    if (cell === undefined) {
      cell = counts.length;
      keys.set(key, cell);
      hexQ.push(q);
      hexR.push(r);
      sumX.push(0);
      sumY.push(0);
      counts.push(0);
    }
    cellOf[i] = cell;
    sumX[cell] += x;
    sumY[cell] += y;
    counts[cell] += 1;
  }

  const count = counts.length;
  const cities = Math.max(1, cityCount);
  const perCity = new Uint32Array(count * cities);
  for (let i = 0; i < size; i++) {
    const cell = cellOf[i];
    if (cell >= 0 && cityOf[i] !== NO_CITY && cityOf[i] < cities) perCity[cell * cities + cityOf[i]]++;
  }

  const out: Cells = {
    count,
    spacing,
    x: new Float32Array(count),
    y: new Float32Array(count),
    n: new Uint32Array(count),
    city: new Uint8Array(count).fill(NO_CITY),
    cellOf,
    maxN: 0,
    refN: 1,
  };
  for (let c = 0; c < count; c++) {
    const n = counts[c];
    const [hx, hy] = hexCenter(hexQ[c], hexR[c], spacing);
    out.x[c] = sumX[c] / n + (hx - sumX[c] / n) * snap;
    out.y[c] = sumY[c] / n + (hy - sumY[c] / n) * snap;
    out.n[c] = n;
    let best = 0;
    for (let k = 0; k < cities; k++) {
      if (perCity[c * cities + k] > best) {
        best = perCity[c * cities + k];
        out.city[c] = k;
      }
    }
    if (n > out.maxN) out.maxN = n;
  }
  if (count) {
    const sorted = out.n.toSorted(); // typed arrays sort numerically
    out.refN = Math.max(1, sorted[Math.min(count - 1, Math.floor(count * 0.95))]);
  }
  return out;
}

/** Per-cell counts of each status code, as [cell * CODES + code]. */
export function aggregateCodes(cells: Cells, codes: Uint8Array, out?: Uint32Array): Uint32Array {
  const len = cells.count * CODES;
  const counts = out && out.length === len ? out : new Uint32Array(len);
  counts.fill(0);
  const n = Math.min(codes.length, cells.cellOf.length);
  for (let i = 0; i < n; i++) {
    const cell = cells.cellOf[i];
    if (cell >= 0) counts[cell * CODES + codes[i]]++;
  }
  return counts;
}

/**
 * The cell spacing for a map this many CSS pixels tall: about 95 cells from top to bottom, 6-13 px.
 * The fleet sits in four metros that are small on a statewide map, so bold dots read better than fine ones.
 */
export function spacingFor(mapHeight: number): number {
  return Math.max(6, Math.min(13, mapHeight / 95));
}

/** Dot radius for `n` batteries: area proportional to n, with the reference cell nearly filling its hex. */
export function dotRadius(n: number, cells: Cells): number {
  const rRef = cells.spacing * 0.52;
  return Math.min(rRef * 1.2, rRef * Math.sqrt(n / cells.refN));
}

/**
 * The nearest dot to (px, py) within `slop` pixels of its edge, or -1. A linear scan: a few
 * thousand cells take microseconds.
 */
export function nearestCell(cells: Cells, px: number, py: number, slop = 6): number {
  let best = -1;
  let bestD = Infinity;
  for (let c = 0; c < cells.count; c++) {
    const dx = cells.x[c] - px;
    const dy = cells.y[c] - py;
    const d = Math.sqrt(dx * dx + dy * dy) - Math.max(dotRadius(cells.n[c], cells), 2);
    if (d <= slop && d < bestD) {
      bestD = d;
      best = c;
    }
  }
  return best;
}
