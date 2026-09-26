import { describe, expect, it } from 'vitest';
import { projectLonLat } from './albers.ts';
import { fitBBox, toScreenX, toScreenY, type Viewport } from './geometry.ts';
import { TEXAS_BBOX } from './texasOutline.ts';
import { easeInOut, lerpViewport, screenMap, triangleBBox, triangleViewport, zoomPadding } from './viewports.ts';

/** Batteries around the four metro centres (lon, lat), a few km apart, like the fleet layout. */
function metros(): { x: Float64Array; y: Float64Array } {
  const centres: [number, number][] = [
    [-95.37, 29.76], // Houston
    [-96.97, 32.82], // Dallas–Fort Worth
    [-98.49, 29.42], // San Antonio
    [-97.74, 30.27], // Austin
  ];
  const pts: [number, number][] = [];
  for (const [lon, lat] of centres) {
    for (let i = -5; i <= 5; i++) for (let j = -5; j <= 5; j++) pts.push(projectLonLat(lon + i * 0.05, lat + j * 0.05));
  }
  pts.push([Number.NaN, Number.NaN]); // a battery with no position
  return { x: Float64Array.from(pts.map((p) => p[0])), y: Float64Array.from(pts.map((p) => p[1])) };
}

const W = 1280;
const H = 712;
const home = fitBBox(TEXAS_BBOX, W, H, { top: 16, right: 16, bottom: 28, left: 16 }, 0.38);

describe('the Texas Triangle box', () => {
  const { x, y } = metros();
  const box = triangleBBox(x, y);

  it('covers the four metros with a margin, and ignores batteries with no position', () => {
    expect(box).not.toBeNull();
    if (!box) return;
    for (let i = 0; i < x.length; i++) {
      if (!Number.isFinite(x[i])) continue;
      expect(x[i]).toBeGreaterThan(box.minX);
      expect(x[i]).toBeLessThan(box.maxX);
      expect(y[i]).toBeGreaterThan(box.minY);
      expect(y[i]).toBeLessThan(box.maxY);
    }
    // Well inside Texas: roughly a third of its width and height.
    expect(box.maxX - box.minX).toBeLessThan(0.45 * (TEXAS_BBOX.maxX - TEXAS_BBOX.minX));
    expect(box.maxY - box.minY).toBeLessThan(0.45 * (TEXAS_BBOX.maxY - TEXAS_BBOX.minY));
  });

  it('is null without positions, and not stretched by one stray battery', () => {
    expect(triangleBBox(new Float64Array(0), new Float64Array(0))).toBeNull();
    const stray = metros();
    const xs = Float64Array.from([...stray.x, ...Array(3000).fill(0).map((_, i) => stray.x[i % 400])]);
    const ys = Float64Array.from([...stray.y, ...Array(3000).fill(0).map((_, i) => stray.y[i % 400])]);
    const far = projectLonLat(-106.4, 31.8); // El Paso
    xs[0] = far[0];
    ys[0] = far[1];
    const trimmed = triangleBBox(xs, ys);
    expect(trimmed?.minX).toBeGreaterThan(far[0] + 100_000);
  });
});

describe('the zoomed-in view', () => {
  const { x, y } = metros();
  const box = triangleBBox(x, y);
  if (!box) throw new Error('no box');
  const pad = zoomPadding(W, H, 296, false);
  const tri = triangleViewport(box, W, H, pad);

  it('is centred on screen between the corner panels, and 2-4 times closer than all of Texas', () => {
    expect(pad.left).toBe(pad.right);
    expect(pad.left).toBeGreaterThanOrEqual(296 + 24);
    const cx = tri.left + tri.width / 2;
    expect(cx).toBeCloseTo(W / 2, 0);
    expect(tri.left).toBeGreaterThanOrEqual(pad.left - 0.5);
    expect(tri.left + tri.width).toBeLessThanOrEqual(W - pad.right + 0.5);
    const zoom = tri.scale / home.scale;
    expect(zoom).toBeGreaterThan(2);
    expect(zoom).toBeLessThan(4);
  });

  it('keeps every metro battery on screen, clear of the side panels', () => {
    for (let i = 0; i < x.length; i++) {
      if (!Number.isFinite(x[i])) continue;
      const sx = toScreenX(tri, x[i]);
      const sy = toScreenY(tri, y[i]);
      expect(sx).toBeGreaterThan(pad.left);
      expect(sx).toBeLessThan(W - pad.right);
      expect(sy).toBeGreaterThan(pad.top);
      expect(sy).toBeLessThan(H - pad.bottom);
    }
  });

  it('uses small margins when the panels stack under a narrow map', () => {
    expect(zoomPadding(700, 600, 296, true)).toEqual({ top: 56, right: 16, bottom: 24, left: 16 });
    // Never more than a third of a narrow window on each side.
    expect(zoomPadding(900, 700, 400, false).left).toBeLessThanOrEqual(900 * 0.34);
  });
});

/** The scale and offsets of a view, rounded, to compare two views. */
const view = (v: Viewport) => [v.scale.toPrecision(9), v.offsetX.toFixed(4), v.offsetY.toFixed(4)];

describe('the zoom animation', () => {
  const { x, y } = metros();
  const box = triangleBBox(x, y);
  if (!box) throw new Error('no box');
  const tri = triangleViewport(box, W, H, zoomPadding(W, H, 296, false));

  it('starts at one view and ends at the other', () => {
    expect(view(lerpViewport(home, tri, 0))).toEqual(view(home));
    expect(view(lerpViewport(home, tri, 1))).toEqual(view(tri));
    expect(view(lerpViewport(tri, home, 1))).toEqual(view(home));
  });

  it('is a pure scaling about one fixed screen point, geometric in scale (no sideways slide)', () => {
    const m = screenMap(home, tri);
    const fixed = { x: m.ox / (1 - m.k), y: m.oy / (1 - m.k) };
    const half = lerpViewport(home, tri, 0.5);
    expect(half.scale).toBeCloseTo(Math.sqrt(home.scale * tri.scale), 9);
    // The world point under the fixed screen point stays there all the way.
    const wx = (fixed.x - home.offsetX) / home.scale;
    const wy = (home.offsetY - fixed.y) / home.scale;
    for (const t of [0.25, 0.5, 0.75]) {
      const v = lerpViewport(home, tri, t);
      expect(toScreenX(v, wx)).toBeCloseTo(fixed.x, 6);
      expect(toScreenY(v, wy)).toBeCloseTo(fixed.y, 6);
    }
  });

  it('maps dots drawn for one view into another', () => {
    const m = screenMap(home, tri);
    const [px, py] = projectLonLat(-97.74, 30.27); // Austin
    expect(m.k * toScreenX(home, px) + m.ox).toBeCloseTo(toScreenX(tri, px), 6);
    expect(m.k * toScreenY(home, py) + m.oy).toBeCloseTo(toScreenY(tri, py), 6);
  });

  it('pans when the scale does not change', () => {
    const moved = { ...home, offsetX: home.offsetX + 100 };
    expect(lerpViewport(home, moved, 0.5).offsetX).toBeCloseTo(home.offsetX + 50, 9);
  });

  it('eases in and out', () => {
    expect(easeInOut(0)).toBe(0);
    expect(easeInOut(1)).toBe(1);
    expect(easeInOut(0.5)).toBeCloseTo(0.5, 9);
    expect(easeInOut(0.1)).toBeLessThan(0.1);
    expect(easeInOut(0.9)).toBeGreaterThan(0.9);
    expect(easeInOut(-1)).toBe(0);
    expect(easeInOut(2)).toBe(1);
  });
});
