import { describe, expect, it } from 'vitest';
import { projectLonLat } from './albers.ts';
import { pointInRing, ringArea, ringBBox, ringSelfIntersects } from './geometry.ts';
import { lonLatToOutlineFrame, OUTLINE_FRAME, outlineFrameRing, outlinePathData, toOutlineFrame } from './outlinePath.ts';
import { TEXAS_BBOX, TEXAS_OUTLINE } from './texasOutline.ts';

describe('the outline frame (km from the north-west corner, y down)', () => {
  it('is the outline bounding box in kilometres', () => {
    expect(OUTLINE_FRAME.width).toBeCloseTo((TEXAS_BBOX.maxX - TEXAS_BBOX.minX) / 1000, 9);
    expect(OUTLINE_FRAME.height).toBeCloseTo((TEXAS_BBOX.maxY - TEXAS_BBOX.minY) / 1000, 9);
    expect(toOutlineFrame(TEXAS_BBOX.minX, TEXAS_BBOX.maxY)).toEqual([0, 0]);
    const [x, y] = toOutlineFrame(TEXAS_BBOX.maxX, TEXAS_BBOX.minY);
    expect(x).toBeCloseTo(OUTLINE_FRAME.width, 9);
    expect(y).toBeCloseTo(OUTLINE_FRAME.height, 9);
  });

  it('flips north to the top: Amarillo is above Brownsville, El Paso left of Houston', () => {
    expect(lonLatToOutlineFrame(-101.83, 35.22)[1]).toBeLessThan(lonLatToOutlineFrame(-97.5, 25.93)[1]);
    expect(lonLatToOutlineFrame(-106.44, 31.79)[0]).toBeLessThan(lonLatToOutlineFrame(-95.37, 29.76)[0]);
  });

  it('uses the same projection as the map', () => {
    const [mx, my] = projectLonLat(-97.74, 30.27);
    expect(lonLatToOutlineFrame(-97.74, 30.27)).toEqual(toOutlineFrame(mx, my));
  });
});

describe('the outline ring in the frame', () => {
  const ring = outlineFrameRing();

  it('keeps every point, the shape and the area (in km²)', () => {
    expect(ring.length).toBe(TEXAS_OUTLINE.length);
    expect(ringSelfIntersects(ring)).toBe(false);
    expect(Math.abs(ringArea(ring))).toBeCloseTo(Math.abs(ringArea(TEXAS_OUTLINE)) / 1e6, 3);
    const box = ringBBox(ring);
    expect(box.minX).toBeCloseTo(0, 9);
    expect(box.minY).toBeCloseTo(0, 9);
    expect(box.maxX).toBeCloseTo(OUTLINE_FRAME.width, 9);
    expect(box.maxY).toBeCloseTo(OUTLINE_FRAME.height, 9);
  });

  it.each([
    ['Houston', -95.37, 29.76],
    ['Dallas', -96.8, 32.78],
    ['Austin', -97.74, 30.27],
    ['San Antonio', -98.49, 29.42],
  ])('has %s inside', (_name, lon, lat) => {
    expect(pointInRing(...lonLatToOutlineFrame(lon, lat), ring)).toBe(true);
  });

  it('has the Gulf of Mexico and Oklahoma City outside', () => {
    expect(pointInRing(...lonLatToOutlineFrame(-94.5, 28.2), ring)).toBe(false);
    expect(pointInRing(...lonLatToOutlineFrame(-97.52, 35.47), ring)).toBe(false);
  });
});

describe('outlinePathData', () => {
  const d = outlinePathData();

  it('is one closed path with a command per point, to 0.1 km', () => {
    expect(d.startsWith('M')).toBe(true);
    expect(d.endsWith('Z')).toBe(true);
    expect(d.match(/[ML]/g)).toHaveLength(TEXAS_OUTLINE.length / 2);
    expect(d.match(/Z/g)).toHaveLength(1);
    for (const n of d.match(/-?\d+(\.\d+)?/g) ?? []) expect(n.split('.')[1]?.length ?? 0).toBeLessThanOrEqual(1);
  });

  it('stays inside the viewBox', () => {
    const numbers = (d.match(/-?\d+(\.\d+)?/g) ?? []).map(Number);
    const xs = numbers.filter((_, i) => i % 2 === 0);
    const ys = numbers.filter((_, i) => i % 2 === 1);
    expect(Math.min(...xs)).toBeGreaterThanOrEqual(0);
    expect(Math.min(...ys)).toBeGreaterThanOrEqual(0);
    expect(Math.max(...xs)).toBeLessThanOrEqual(OUTLINE_FRAME.width + 0.05);
    expect(Math.max(...ys)).toBeLessThanOrEqual(OUTLINE_FRAME.height + 0.05);
  });

  it('is small (a few KB) and computed once', () => {
    expect(d.length).toBeLessThan(6000);
    expect(outlinePathData()).toBe(d);
  });
});
