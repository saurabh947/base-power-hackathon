// The map's one zoom level (docs/UX.md, "Live map v2"): it loads showing all of Texas, and "+", a
// double-click or the keyboard zooms to the Texas Triangle, the bounding box of the four metros' batteries
// with a margin, centred on screen between the corner panels. The zoom animates as a pure scaling about the
// one screen point both views share, so nothing slides sideways. Pure, so it is unit tested.

import { fitBBox, type BBox, type Padding, type Viewport } from './geometry.ts';

/** Margin around the metros' batteries: a share of the larger side, at least this many metres. */
export const TRIANGLE_MARGIN = 0.08;
export const TRIANGLE_MIN_MARGIN_M = 20_000;
/** How long the zoom takes. */
export const ZOOM_MS = 650;

/**
 * The Texas Triangle: the box around the fleet's batteries (EPSG:3083 metres), ignoring the farthest 0.2% on
 * each side so one stray position cannot stretch it, plus a margin. Null without positions.
 */
export function triangleBBox(x: ArrayLike<number>, y: ArrayLike<number>, trim = 0.002): BBox | null {
  const xs: number[] = [];
  const ys: number[] = [];
  for (let i = 0; i < x.length; i++) {
    if (Number.isFinite(x[i]) && Number.isFinite(y[i])) {
      xs.push(x[i]);
      ys.push(y[i]);
    }
  }
  if (!xs.length) return null;
  xs.sort((a, b) => a - b);
  ys.sort((a, b) => a - b);
  const k = Math.min(xs.length - 1, Math.floor(xs.length * trim));
  const minX = xs[k];
  const maxX = xs[xs.length - 1 - k];
  const minY = ys[k];
  const maxY = ys[ys.length - 1 - k];
  const margin = Math.max(TRIANGLE_MIN_MARGIN_M, TRIANGLE_MARGIN * Math.max(maxX - minX, maxY - minY));
  return { minX: minX - margin, minY: minY - margin, maxX: maxX + margin, maxY: maxY + margin };
}

/** The zoomed-in view: the Triangle centred between `padding` (which keeps it clear of the corner panels). */
export function triangleViewport(bbox: BBox, width: number, height: number, padding: Padding): Viewport {
  return fitBBox(bbox, width, height, padding, 0.5);
}

/** The screen map from one viewport to another: a point p drawn for `from` is at k·p + o in `to`. */
export interface ScreenMap {
  k: number;
  ox: number;
  oy: number;
}

export function screenMap(from: Viewport, to: Viewport): ScreenMap {
  const k = to.scale / from.scale;
  return { k, ox: to.offsetX - k * from.offsetX, oy: to.offsetY - k * from.offsetY };
}

/**
 * The view `t` (0-1) of the way from `a` to `b`: a scaling about the screen point the two views share (the
 * fixed point of their screen map), geometric in scale so the zoom feels even. A pure pan when the scales match.
 */
export function lerpViewport(a: Viewport, b: Viewport, t: number): Viewport {
  const u = Math.min(1, Math.max(0, t));
  const lerp = (p: number, q: number) => p + (q - p) * u;
  const box = { left: lerp(a.left, b.left), top: lerp(a.top, b.top), width: lerp(a.width, b.width), height: lerp(a.height, b.height) };
  const m = screenMap(a, b);
  if (Math.abs(1 - m.k) < 1e-9) {
    return { scale: a.scale, offsetX: lerp(a.offsetX, b.offsetX), offsetY: lerp(a.offsetY, b.offsetY), ...box };
  }
  const px = m.ox / (1 - m.k);
  const py = m.oy / (1 - m.k);
  const kt = m.k ** u;
  return { scale: a.scale * kt, offsetX: px + kt * (a.offsetX - px), offsetY: py + kt * (a.offsetY - py), ...box };
}

/** Smooth start and stop. */
export function easeInOut(t: number): number {
  const u = Math.min(1, Math.max(0, t));
  return u < 0.5 ? 4 * u * u * u : 1 - (-2 * u + 2) ** 3 / 2;
}

/**
 * Room to keep on each side of the zoomed view: the corner panels sit left and right, so the Triangle is
 * centred between them; above and below, a small margin (the scope chip sits over Oklahoma, off the Triangle).
 */
export function zoomPadding(width: number, height: number, cornerWidth: number, stacked: boolean): Padding {
  if (stacked) return { top: 56, right: 16, bottom: 24, left: 16 };
  const side = Math.min(Math.max(16, cornerWidth + 24), Math.max(16, width * 0.34));
  return { top: Math.max(56, Math.round(height * 0.07)), right: side, bottom: Math.max(28, Math.round(height * 0.04)), left: side };
}
