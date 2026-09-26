import { describe, expect, it } from 'vitest';
import { fitBBox, pointInRing, ringArea, ringBBox, ringSelfIntersects, simplifyLine, simplifyRing, toScreenX, toScreenY } from './geometry.ts';

describe('Douglas–Peucker', () => {
  it('drops points within the tolerance and keeps the corners', () => {
    const line = [0, 0, 1, 0.05, 2, -0.05, 3, 0, 3.02, 1, 3, 2];
    expect(simplifyLine(line, 0.1)).toEqual([0, 0, 3, 0, 3, 2]);
    expect(simplifyLine(line, 0.01)).toHaveLength(line.length);
  });

  it('simplifies a closed ring without losing its shape', () => {
    // A 100 x 50 rectangle with a point every unit along its edges.
    const ring: number[] = [];
    for (let x = 0; x < 100; x++) ring.push(x, 0);
    for (let y = 0; y < 50; y++) ring.push(100, y);
    for (let x = 100; x > 0; x--) ring.push(x, 50);
    for (let y = 50; y > 0; y--) ring.push(0, y);
    const simple = simplifyRing(ring, 0.5);
    expect(simple.length / 2).toBe(4);
    expect(Math.abs(ringArea(simple))).toBeCloseTo(5000, 5);
    expect(ringSelfIntersects(simple)).toBe(false);
  });
});

describe('rings', () => {
  const square = [0, 0, 10, 0, 10, 10, 0, 10];

  it('knows inside from outside', () => {
    expect(pointInRing(5, 5, square)).toBe(true);
    expect(pointInRing(15, 5, square)).toBe(false);
    expect(pointInRing(-1, -1, square)).toBe(false);
  });

  it('measures area and bounding box, and spots a bow tie', () => {
    expect(ringArea(square)).toBe(100);
    expect(ringBBox(square)).toEqual({ minX: 0, minY: 0, maxX: 10, maxY: 10 });
    expect(ringSelfIntersects([0, 0, 10, 10, 10, 0, 0, 10])).toBe(true);
  });
});

describe('fitBBox', () => {
  it('fits the largest uniform scale inside the padding, centred, with y flipped for the screen', () => {
    const vp = fitBBox({ minX: 0, minY: 0, maxX: 200, maxY: 100 }, 1000, 600, { top: 50, right: 20, bottom: 50, left: 20 });
    expect(vp.scale).toBeCloseTo(4.8, 10); // width-bound: 960 / 200
    expect(vp.width).toBeCloseTo(960, 6);
    expect(vp.height).toBeCloseTo(480, 6);
    expect(vp.left).toBeCloseTo(20, 6);
    expect(vp.top).toBeCloseTo(60, 6); // (500 - 480) / 2 + 50
    expect(toScreenX(vp, 0)).toBeCloseTo(20, 6);
    expect(toScreenY(vp, 100)).toBeCloseTo(60, 6); // the north edge is at the top
    expect(toScreenY(vp, 0)).toBeCloseTo(540, 6);
  });
});
