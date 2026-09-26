import { describe, expect, it } from 'vitest';
import { aggregateCodes, buildCells, dotRadius, hexCenter, hexOf, nearestCell, spacingFor } from './cells.ts';
import { NO_CITY } from './layout.ts';
import { CODES, DOWN, OFFLINE, SELLING } from './status.ts';

function seeded(seed: number) {
  let a = seed;
  return () => {
    a = (a * 1664525 + 1013904223) % 4294967296;
    return a / 4294967296;
  };
}

describe('hex bins', () => {
  it('put a point in the hex whose centre is nearest', () => {
    const rand = seeded(1);
    for (let k = 0; k < 2000; k++) {
      const x = rand() * 500;
      const y = rand() * 500;
      const [q, r] = hexOf(x, y, 8);
      const [cx, cy] = hexCenter(q, r, 8);
      const d = Math.hypot(x - cx, y - cy);
      // No neighbouring centre is closer.
      for (const [dq, dr] of [[1, 0], [-1, 0], [0, 1], [0, -1], [1, -1], [-1, 1]]) {
        const [nx, ny] = hexCenter(q + dq, r + dr, 8);
        expect(d).toBeLessThanOrEqual(Math.hypot(x - nx, y - ny) + 1e-9);
      }
    }
  });
});

describe('buildCells', () => {
  // 20,000 batteries in two clusters 200 px apart, plus two that are not on screen.
  const rand = seeded(7);
  const size = 20_002;
  const sx = new Float32Array(size);
  const sy = new Float32Array(size);
  const cityOf = new Uint8Array(size);
  for (let i = 0; i < 20_000; i++) {
    const city = i < 12_000 ? 0 : 1;
    cityOf[i] = city;
    const angle = rand() * 2 * Math.PI;
    const radius = 40 * Math.sqrt(rand());
    sx[i] = (city ? 350 : 150) + radius * Math.cos(angle);
    sy[i] = 200 + radius * Math.sin(angle);
  }
  sx[20_000] = Number.NaN;
  sy[20_001] = Number.POSITIVE_INFINITY;
  cityOf[20_000] = cityOf[20_001] = NO_CITY;
  const cells = buildCells(sx, sy, cityOf, 6, 2);

  it('keeps every placed battery in exactly one cell, and at most a few thousand dots', () => {
    const total = Array.from(cells.n).reduce((a, b) => a + b, 0);
    expect(total).toBe(20_000);
    expect(cells.cellOf[20_000]).toBe(-1);
    expect(cells.cellOf[20_001]).toBe(-1);
    expect(cells.count).toBeGreaterThan(50);
    expect(cells.count).toBeLessThan(3000);
    for (let i = 0; i < 20_000; i += 97) expect(cells.n[cells.cellOf[i]]).toBeGreaterThan(0);
  });

  it('draws each dot near its batteries and gives it the city most of them belong to', () => {
    for (let c = 0; c < cells.count; c++) {
      expect(cells.city[c]).toBe(cells.x[c] < 250 ? 0 : 1);
    }
    expect(cells.refN).toBeGreaterThan(1);
    expect(cells.maxN).toBeGreaterThanOrEqual(cells.refN);
  });

  it('sizes dots so AREA follows the count', () => {
    const r1 = dotRadius(cells.refN / 4, cells);
    const r2 = dotRadius(cells.refN, cells);
    expect((r2 / r1) ** 2).toBeCloseTo(4, 5);
    expect(dotRadius(cells.refN * 100, cells)).toBeLessThanOrEqual(cells.spacing * 0.63); // capped
  });

  it('counts status codes per cell', () => {
    const codes = new Uint8Array(size).fill(SELLING);
    codes[0] = OFFLINE;
    codes[1] = DOWN;
    const counts = aggregateCodes(cells, codes);
    expect(counts).toHaveLength(cells.count * CODES);
    let selling = 0;
    let offline = 0;
    let down = 0;
    for (let c = 0; c < cells.count; c++) {
      selling += counts[c * CODES + SELLING];
      offline += counts[c * CODES + OFFLINE];
      down += counts[c * CODES + DOWN];
    }
    expect([selling, offline, down]).toEqual([19_998, 1, 1]);
    expect(counts[cells.cellOf[0] * CODES + OFFLINE]).toBe(1);
  });

  it('finds the dot under the pointer, and nothing over empty map', () => {
    const c = nearestCell(cells, 150, 200);
    expect(c).toBeGreaterThanOrEqual(0);
    expect(cells.city[c]).toBe(0);
    expect(cells.city[nearestCell(cells, 352, 205)]).toBe(1);
    expect(nearestCell(cells, 250, 60)).toBe(-1);
  });

  it('scales cells with the map, within 4-9 px', () => {
    expect(spacingFor(300)).toBe(6);
    expect(spacingFor(760)).toBeCloseTo(8, 5);
    expect(spacingFor(5000)).toBe(13);
  });
});
