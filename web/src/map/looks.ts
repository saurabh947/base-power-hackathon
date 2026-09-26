// How each dot looks (docs/UX.md, "Live map v2"): one meaning per look, and shape as well as colour.
//
//   selling           green, solid, with a soft halo
//   charging          blue, solid
//   waiting           gray, small
//   offline           red ring, hollow (a visitor took the batteries offline)
//   controller down   red, solid and pulsing, until another controller takes the batteries over
//
// A dot stands for the batteries in its cell and takes the look of their MAJORITY. A cell with ANY offline or
// crashed batteries also gets a red edge, wider the larger their share, so an outage is never hidden inside a
// green or blue dot. Ties go to the outage (controller down, then offline), then selling, charging, waiting.
// Pure, so it is unit tested; src/map/renderer.ts draws it and the legend (map.css .code-*) matches it.

import { dotRadius, type Cells } from './cells.ts';
import { CHARGING, CODES, DOWN, OFFLINE, SELLING, WAITING } from './status.ts';

export type Rgb = readonly [number, number, number];

export const PALETTE = {
  /** Selling: the site's money green (--money). */
  selling: [128, 255, 166] as Rgb,
  charging: [84, 160, 255] as Rgb,
  waiting: [132, 140, 150] as Rgb,
  /** Offline rings, crashed dots and every red edge. */
  outage: [255, 90, 90] as Rgb,
  /** The halo under selling dots. */
  glow: [96, 255, 150] as Rgb,
} as const;

/** Dots of other cities while one is selected ("dimmed a little"). */
export const DIM_ALPHA = 0.38;
/** Below this radius a dot keeps the radius and fades instead, so its ink still matches its count. */
export const MIN_RADIUS = 0.9;
/** Waiting dots are drawn smaller: "gray, small". */
export const WAITING_SCALE = 0.6;
/** A hollow ring smaller than this would read as a dot. */
export const RING_MIN_RADIUS = 2.4;

/** Ties go to the first code in this list. */
const PRIORITY = [DOWN, OFFLINE, SELLING, CHARGING, WAITING] as const;

/** The code most of a cell's batteries have (counts laid out as [cell * CODES + code]); -1 for an empty cell. */
export function majority(counts: ArrayLike<number>, base = 0): number {
  let best = -1;
  let bestN = 0;
  for (const code of PRIORITY) {
    const n = counts[base + code];
    if (n > bestN) {
      bestN = n;
      best = code;
    }
  }
  return best;
}

/** Target look of every cell, in parallel arrays (indexed by cell). */
export interface DotLooks {
  radius: Float32Array;
  red: Float32Array;
  green: Float32Array;
  blue: Float32Array;
  /** 1 solid, 0 hollow. */
  fill: Float32Array;
  /** The hollow red ring of an offline majority, 0-1. */
  ring: Float32Array;
  /** The red edge of a cell with some offline or crashed batteries: their share, 0-1 (0 = no edge). */
  edge: Float32Array;
  /** 1 while most of the cell's batteries have lost their controller: the dot itself pulses red. */
  crash: Float32Array;
  /** The share of the cell's batteries that lost their controller: its red edge pulses. */
  down: Float32Array;
  /** The selling halo, 0-1. */
  glow: Float32Array;
  alpha: Float32Array;
}

export const LOOK_KEYS = ['radius', 'red', 'green', 'blue', 'fill', 'ring', 'edge', 'crash', 'down', 'glow', 'alpha'] as const;

export function allocLooks(count: number): DotLooks {
  const out = {} as DotLooks;
  for (const key of LOOK_KEYS) out[key] = new Float32Array(count);
  return out;
}

export interface LookOptions {
  /** Selected city index, or null for all of Texas. */
  selected: number | null;
  /** prefers-reduced-motion: no halos (the renderer also stops the pulses). */
  reducedMotion: boolean;
}

/** One cell's look, for tests and the legend. */
export interface Look {
  code: number;
  color: Rgb;
  radius: number;
  fill: number;
  ring: number;
  edge: number;
  crash: number;
  down: number;
  glow: number;
  alpha: number;
}

/** Fill `out` with each cell's target look from per-cell code counts ([cell * CODES + code]). */
export function computeLooks(cells: Cells, counts: Uint32Array, options: LookOptions, out: DotLooks): void {
  for (let c = 0; c < cells.count; c++) {
    const look = cellLook(cells, counts, c, options);
    out.radius[c] = look.radius;
    out.red[c] = look.color[0];
    out.green[c] = look.color[1];
    out.blue[c] = look.color[2];
    out.fill[c] = look.fill;
    out.ring[c] = look.ring;
    out.edge[c] = look.edge;
    out.crash[c] = look.crash;
    out.down[c] = look.down;
    out.glow[c] = look.glow;
    out.alpha[c] = look.alpha;
  }
}

export function cellLook(cells: Cells, counts: ArrayLike<number>, c: number, options: LookOptions): Look {
  const n = cells.n[c];
  const base = c * CODES;
  const off = counts[base + OFFLINE];
  const down = counts[base + DOWN];
  const code = n > 0 ? majority(counts, base) : WAITING;
  const other = options.selected !== null && cells.city[c] !== options.selected;

  let radius = dotRadius(n, cells);
  let alpha = 1;
  if (radius < MIN_RADIUS) {
    alpha = (radius / MIN_RADIUS) ** 2;
    radius = MIN_RADIUS;
  }

  let color: Rgb = PALETTE.waiting;
  let fill = 1;
  let ring = 0;
  let crash = 0;
  let glow = 0;
  if (code === SELLING) {
    color = PALETTE.selling;
    glow = options.reducedMotion ? 0 : other ? 0.25 : 1;
  } else if (code === CHARGING) {
    color = PALETTE.charging;
  } else if (code === OFFLINE) {
    color = PALETTE.outage;
    fill = 0;
    ring = 1;
    radius = Math.max(radius, RING_MIN_RADIUS);
  } else if (code === DOWN) {
    color = PALETTE.outage;
    crash = 1;
  } else {
    radius = Math.max(MIN_RADIUS, radius * WAITING_SCALE);
  }
  const outage = code === OFFLINE || code === DOWN;
  return {
    code,
    color,
    radius,
    fill,
    ring,
    edge: !outage && n > 0 && off + down > 0 ? (off + down) / n : 0,
    crash,
    down: n > 0 ? down / n : 0,
    glow,
    alpha: alpha * (other ? DIM_ALPHA : 1),
  };
}

/** The red edge's width in CSS px for an outage share (0-1): at least 1 px, up to 2.2 px. */
export function edgeWidth(share: number): number {
  return share > 0 ? 1 + 1.2 * Math.min(1, share) : 0;
}

/**
 * The red edge's strength for an outage share: always visible, stronger as the share grows, full from half
 * the batteries up (beyond that the dot itself turns into a red ring).
 */
export function edgeAlpha(share: number): number {
  return share > 0 ? Math.min(1, 0.45 + 1.1 * share) : 0;
}
