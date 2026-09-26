// How each dot looks (docs/UX.md, Live map), from its cell's status counts:
//
//   1 waiting   green, medium brightness
//   2 selling   bright green with a soft glow
//   3 charging  green with a thin light ring
//   0 offline   dim gray
//   4 down      the dot flashes dark, then returns to green when it is handed over
//
// A cell mixes its batteries' looks by count: 30% offline makes a dot 30% of the
// way to gray. A cell with any battery whose controller is down flashes dark;
// the more of them, the darker. Pure, so it is unit tested.

import { dotRadius, type Cells } from './cells.ts';
import { CHARGING, CODES, DOWN, OFFLINE, SELLING, WAITING } from './status.ts';

export type Rgb = readonly [number, number, number];

export const PALETTE = {
  waiting: [64, 168, 104] as Rgb,
  selling: [128, 255, 166] as Rgb,
  charging: [84, 204, 128] as Rgb,
  offline: [88, 94, 101] as Rgb,
  down: [9, 11, 13] as Rgb,
  ring: [222, 255, 232] as Rgb,
  glow: [96, 255, 150] as Rgb,
} as const;

/** Dots of other cities while one is selected ("dimmed a little"). */
export const DIM_ALPHA = 0.38;
/** Below this radius a dot keeps the radius and fades instead, so its ink still matches its count. */
export const MIN_RADIUS = 0.9;

/** Target look of every cell, in parallel arrays (indexed by cell). */
export interface DotTargets {
  radius: Float32Array;
  red: Float32Array;
  green: Float32Array;
  blue: Float32Array;
  alpha: Float32Array;
  glow: Float32Array;
  ring: Float32Array;
  dark: Float32Array;
}

export function allocTargets(count: number): DotTargets {
  return {
    radius: new Float32Array(count),
    red: new Float32Array(count),
    green: new Float32Array(count),
    blue: new Float32Array(count),
    alpha: new Float32Array(count),
    glow: new Float32Array(count),
    ring: new Float32Array(count),
    dark: new Float32Array(count),
  };
}

/** How dark a dot gets when `down` of its `n` batteries lost their controller (0 when none). */
export function darkness(down: number, n: number): number {
  if (down <= 0 || n <= 0) return 0;
  return Math.min(1, 0.5 + 0.5 * Math.sqrt(down / n));
}

export interface StyleOptions {
  /** Selected city index, or null for all of Texas. */
  selected: number | null;
  /** prefers-reduced-motion: no glows. */
  reducedMotion: boolean;
}

/** Fill `out` with each cell's target look from per-cell code counts ([cell * CODES + code]). */
export function computeTargets(cells: Cells, counts: Uint32Array, options: StyleOptions, out: DotTargets): void {
  const { waiting, selling, charging, offline } = PALETTE;
  for (let c = 0; c < cells.count; c++) {
    const n = cells.n[c];
    const base = c * CODES;
    const cOff = counts[base + OFFLINE];
    const cWait = counts[base + WAITING];
    const cSell = counts[base + SELLING];
    const cCharge = counts[base + CHARGING];
    const cDown = counts[base + DOWN];
    const online = cWait + cSell + cCharge;
    const shown = online + cOff; // down batteries only darken the dot

    // A cell whose online batteries are all down shows green under the flash: that is what comes back.
    let r: number = waiting[0];
    let g: number = waiting[1];
    let b: number = waiting[2];
    if (online > 0) {
      r = (cWait * waiting[0] + cSell * selling[0] + cCharge * charging[0]) / online;
      g = (cWait * waiting[1] + cSell * selling[1] + cCharge * charging[1]) / online;
      b = (cWait * waiting[2] + cSell * selling[2] + cCharge * charging[2]) / online;
    }
    const fOff = shown > 0 ? cOff / shown : 0;
    out.red[c] = r + (offline[0] - r) * fOff;
    out.green[c] = g + (offline[1] - g) * fOff;
    out.blue[c] = b + (offline[2] - b) * fOff;

    let radius = dotRadius(n, cells);
    let alpha = 1;
    if (radius < MIN_RADIUS) {
      alpha = (radius / MIN_RADIUS) ** 2;
      radius = MIN_RADIUS;
    }
    const other = options.selected !== null && cells.city[c] !== options.selected;
    out.radius[c] = radius;
    out.alpha[c] = alpha * (other ? DIM_ALPHA : 1);
    out.glow[c] = options.reducedMotion || n === 0 ? 0 : (cSell / n) * (other ? 0.25 : 1);
    out.ring[c] = n === 0 ? 0 : (cCharge / n) * (other ? 0.5 : 1);
    out.dark[c] = darkness(cDown, n);
  }
}
