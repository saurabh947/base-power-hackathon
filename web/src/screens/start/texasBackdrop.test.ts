import { describe, expect, it } from 'vitest';
import { DEFAULT_CITIES, type City } from '../../lib/cities.ts';
import { pointInRing } from '../../map/geometry.ts';
import { OUTLINE_FRAME, outlineFrameRing } from '../../map/outlinePath.ts';
import { backdropCities, DOT_SPACING_KM } from './texasBackdrop.ts';

const clusters = backdropCities(DEFAULT_CITIES);
const byLocation = Object.fromEntries(clusters.map((c) => [c.location, c]));

describe('the Start page backdrop clusters', () => {
  it('has one cluster per Texas Triangle city, in the given order', () => {
    expect(clusters.map((c) => c.location)).toEqual(DEFAULT_CITIES.map((c) => c.location));
  });

  it('puts each cluster on its metro, inside the frame', () => {
    for (const c of clusters) {
      expect(c.x).toBeGreaterThan(0);
      expect(c.x).toBeLessThan(OUTLINE_FRAME.width);
      expect(c.y).toBeGreaterThan(0);
      expect(c.y).toBeLessThan(OUTLINE_FRAME.height);
    }
    // Dallas–Fort Worth is north of Austin, Houston east of San Antonio.
    expect(byLocation.LZ_NORTH.y).toBeLessThan(byLocation.LZ_AEN.y);
    expect(byLocation.LZ_HOUSTON.x).toBeGreaterThan(byLocation.LZ_CPS.x);
  });

  it('gives bigger metros more dots, and every city some', () => {
    for (const c of clusters) expect(c.dots.length).toBeGreaterThan(5);
    expect(byLocation.LZ_NORTH.dots.length).toBeGreaterThan(byLocation.LZ_AEN.dots.length);
    expect(byLocation.LZ_HOUSTON.dots.length).toBeGreaterThan(byLocation.LZ_CPS.dots.length);
    expect(byLocation.LZ_NORTH.glow).toBeGreaterThan(byLocation.LZ_AEN.glow);
  });

  it('keeps every dot on land, near its city, and apart from its neighbours', () => {
    const ring = outlineFrameRing();
    const all = clusters.flatMap((c) => c.dots.map((d) => ({ ...d, city: c })));
    for (const d of all) {
      expect(pointInRing(d.x, d.y, ring)).toBe(true);
      expect(Math.hypot(d.x - d.city.x, d.y - d.city.y)).toBeLessThan(d.city.glow);
      expect(d.r).toBeLessThanOrEqual(DOT_SPACING_KM / 2);
      expect(d.opacity).toBeGreaterThan(0);
      expect(d.opacity).toBeLessThanOrEqual(1);
    }
    const seen = new Set(all.map((d) => `${d.x},${d.y}`));
    expect(seen.size).toBe(all.length);
    expect(all.length).toBeLessThan(400);
  });

  it('makes the middle of a cluster its biggest dot', () => {
    const dfw = byLocation.LZ_NORTH;
    const nearest = dfw.dots.reduce((a, b) => (Math.hypot(a.x - dfw.x, a.y - dfw.y) <= Math.hypot(b.x - dfw.x, b.y - dfw.y) ? a : b));
    expect(Math.max(...dfw.dots.map((d) => d.r))).toBeCloseTo(nearest.r, 0);
  });

  it('leaves out cities with no centre or no batteries, and is stable', () => {
    const odd: City[] = [
      { ...DEFAULT_CITIES[0], location: 'LZ_NOWHERE', center: [Number.NaN, Number.NaN] },
      { ...DEFAULT_CITIES[1], location: 'LZ_EMPTY', batteries: 0 },
      DEFAULT_CITIES[2],
    ];
    expect(backdropCities(odd).map((c) => c.location)).toEqual([DEFAULT_CITIES[2].location]);
    expect(backdropCities([])).toEqual([]);
    expect(backdropCities(DEFAULT_CITIES)).toEqual(clusters);
  });

  it('shares one grid between neighbouring metros, so their dots never overlap', () => {
    const near: City[] = [
      { ...DEFAULT_CITIES[3], location: 'A', center: [-97.74, 30.27] },
      { ...DEFAULT_CITIES[3], location: 'B', center: [-97.6, 30.35] },
    ];
    const [a, b] = backdropCities(near);
    const keys = new Set(a.dots.map((d) => `${d.x},${d.y}`));
    for (const d of b.dots) expect(keys.has(`${d.x},${d.y}`)).toBe(false);
  });
});
