import { describe, expect, it } from 'vitest';
import { projectLonLat } from './albers.ts';
import { pointInRing, ringArea, ringBBox, ringSelfIntersects } from './geometry.ts';
import { TEXAS_BBOX, TEXAS_OUTLINE, TEXAS_OUTLINE_SOURCE } from './texasOutline.ts';

const inside = (lon: number, lat: number) => pointInRing(...projectLonLat(lon, lat), TEXAS_OUTLINE);

describe('the bundled Texas outline', () => {
  it('is a light, simple polygon with its source and licence noted', () => {
    const points = TEXAS_OUTLINE.length / 2;
    expect(points).toBe(TEXAS_OUTLINE_SOURCE.points);
    expect(points).toBeGreaterThan(200);
    expect(points).toBeLessThan(600);
    expect(ringSelfIntersects(TEXAS_OUTLINE)).toBe(false);
    expect(TEXAS_OUTLINE_SOURCE.licence).toMatch(/public domain/i);
    expect(TEXAS_OUTLINE_SOURCE.url).toMatch(/^https:\/\/www2\.census\.gov\//);
    expect(ringBBox(TEXAS_OUTLINE)).toEqual(TEXAS_BBOX);
  });

  it('keeps the area of Texas (equal-area projection): within 1.5% of the Census 695,668 km²', () => {
    const km2 = Math.abs(ringArea(TEXAS_OUTLINE)) / 1e6;
    expect(Math.abs(km2 - 695_668) / 695_668).toBeLessThan(0.015);
  });

  it.each([
    ['Houston', -95.37, 29.76],
    ['Dallas', -96.8, 32.78],
    ['Fort Worth', -97.33, 32.75],
    ['Austin', -97.74, 30.27],
    ['San Antonio', -98.49, 29.42],
    ['Amarillo', -101.83, 35.22],
    ['Brownsville', -97.5, 25.93],
  ])('has %s inside', (_name, lon, lat) => {
    expect(inside(lon, lat)).toBe(true);
  });

  it('has El Paso inside the western tip', () => {
    expect(inside(-106.44, 31.79)).toBe(true);
    const [x] = projectLonLat(-106.44, 31.79);
    // The westernmost 3% of the state's width.
    expect((x - TEXAS_BBOX.minX) / (TEXAS_BBOX.maxX - TEXAS_BBOX.minX)).toBeLessThan(0.03);
  });

  it.each([
    ['Oklahoma City', -97.52, 35.47],
    ['Albuquerque', -106.65, 35.08],
    ['Shreveport', -93.75, 32.52],
    ['Lake Charles', -93.22, 30.23],
    ['Monterrey', -100.32, 25.69],
    ['Chihuahua', -106.09, 28.63],
    ['the Gulf of Mexico', -94.5, 28.2],
  ])('has %s outside', (_name, lon, lat) => {
    expect(inside(lon, lat)).toBe(false);
  });
});
