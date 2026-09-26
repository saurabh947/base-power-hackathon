import { describe, expect, it } from 'vitest';
import { albersEqualArea, projectLonLat, texasAlbers, TEXAS_ALBERS_PARAMS } from './albers.ts';

const RAD = Math.PI / 180;

describe('Albers equal-area conic (ellipsoidal)', () => {
  it('matches the PROJ reference values (builtins.gie: aea, GRS80, lat_1=0, lat_2=2) to 0.1 mm', () => {
    const p = albersEqualArea({ ...TEXAS_ALBERS_PARAMS, lat1: 0, lat2: 2, lat0: 0, lon0: 0, falseEasting: 0, falseNorthing: 0 });
    const cases: [number, number, number, number][] = [
      [2, 1, 222571.608757106, 110653.32674303],
      [2, -1, 222706.306508391, -110484.2671444],
      [-2, 1, -222571.608757106, 110653.32674303],
      [-2, -1, -222706.306508391, -110484.2671444],
    ];
    for (const [lon, lat, x, y] of cases) {
      const [px, py] = p.forward(lon, lat);
      expect(Math.abs(px - x)).toBeLessThan(1e-4);
      expect(Math.abs(py - y)).toBeLessThan(1e-4);
    }
  });

  it('uses the EPSG:3083 false origin: 100°W 18°N is (1,500,000, 6,000,000)', () => {
    const [x, y] = projectLonLat(-100, 18);
    expect(x).toBeCloseTo(1_500_000, 6);
    expect(y).toBeCloseTo(6_000_000, 6);
    // The central meridian is vertical.
    expect(projectLonLat(-100, 31)[0]).toBeCloseTo(1_500_000, 6);
  });

  it('is true to scale along both standard parallels (27.5°N and 35°N), and close to it between them', () => {
    expect(texasAlbers.parallelScale(27.5)).toBeCloseTo(1, 12);
    expect(texasAlbers.parallelScale(35)).toBeCloseTo(1, 12);
    expect(texasAlbers.parallelScale(31)).toBeGreaterThan(0.995);
    expect(texasAlbers.parallelScale(31)).toBeLessThan(1);
  });

  it('preserves area: the projected area element equals the ellipsoid area element', () => {
    const { a, invF } = TEXAS_ALBERS_PARAMS;
    const f = 1 / invF;
    const e2 = 2 * f - f * f;
    for (const [lon, lat] of [
      [-106, 26],
      [-100, 31],
      [-94, 36],
    ]) {
      const d = 1e-4;
      const [x0, y0] = projectLonLat(lon, lat);
      const [x1, y1] = projectLonLat(lon + d, lat);
      const [x2, y2] = projectLonLat(lon, lat + d);
      const projected = Math.abs((x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0));
      const phi = lat * RAD;
      const ellipsoid = (a * a * (1 - e2) * Math.cos(phi) * (d * RAD) ** 2) / (1 - e2 * Math.sin(phi) ** 2) ** 2;
      expect(projected / ellipsoid).toBeCloseTo(1, 5);
    }
  });

  it('inverts exactly', () => {
    for (const [lon, lat] of [
      [-95.37, 29.76],
      [-106.49, 31.76],
      [-93.51, 36.5],
    ]) {
      const [x, y] = projectLonLat(lon, lat);
      const [lon2, lat2] = texasAlbers.inverse(x, y);
      expect(lon2).toBeCloseTo(lon, 9);
      expect(lat2).toBeCloseTo(lat, 9);
    }
  });

  it('puts east to the right and north up', () => {
    const houston = projectLonLat(-95.37, 29.76);
    const dallas = projectLonLat(-96.8, 32.78);
    expect(houston[0]).toBeGreaterThan(dallas[0]);
    expect(dallas[1]).toBeGreaterThan(houston[1]);
  });
});
