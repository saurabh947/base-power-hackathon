import { describe, expect, it } from 'vitest';
import { projectLonLat } from './albers.ts';
import { decodePositions, NO_CITY, readLayout } from './layout.ts';

/** positions for [lon, lat] pairs: lon = -100 + v/1000, lat = 31 + v/1000, little-endian Int16. */
function encode(pairs: [number, number][]): string {
  const bytes = new Uint8Array(pairs.length * 4);
  const view = new DataView(bytes.buffer);
  pairs.forEach(([lon, lat], i) => {
    view.setInt16(4 * i, Math.round((lon + 100) * 1000), true);
    view.setInt16(4 * i + 2, Math.round((lat - 31) * 1000), true);
  });
  return btoa(String.fromCharCode(...bytes));
}

describe('GET /fleet/layout positions', () => {
  it('decode little-endian Int16 pairs with lon = -100 + v/1000 and lat = 31 + v/1000', () => {
    const out = decodePositions(encode([[-95.37, 29.76], [-106.44, 31.79], [-100, 31]]), 3);
    expect(out).not.toBeNull();
    expect(Array.from(out ?? []).map((v) => Math.round(v * 1000) / 1000)).toEqual([-95.37, 29.76, -106.44, 31.79, -100, 31]);
    // Byte order: -95.37 is v = 4630 = 0x1216, sent as 0x16 0x12.
    const raw = atob(encode([[-95.37, 29.76]]));
    expect([raw.charCodeAt(0), raw.charCodeAt(1)]).toEqual([0x16, 0x12]);
  });

  it('refuse a positions field that is too short', () => {
    expect(decodePositions(encode([[-95, 30]]), 2)).toBeNull();
  });
});

describe('readLayout', () => {
  const raw = {
    version: 'v1',
    fleet_size: 4,
    cities: [
      { location: 'LZ_NORTH', name: 'Dallas–Fort Worth', short: 'DFW', batteries: 2, first_battery: 2, center_lonlat: [-96.97, 32.82] },
      { location: 'LZ_HOUSTON', name: 'Houston', short: 'HOU', batteries: 2, first_battery: 0, center_lonlat: [-95.37, 29.76] },
    ],
    positions: encode([
      [-95.37, 29.76],
      [-95.5, 29.8],
      [-96.8, 32.78],
      [-97.33, 32.75],
    ]),
  };

  it('projects every battery with EPSG:3083 and knows its city (cities in id order)', () => {
    const layout = readLayout(raw);
    expect(layout?.cities.map((c) => c.location)).toEqual(['LZ_HOUSTON', 'LZ_NORTH']);
    expect(Array.from(layout?.cityOf ?? [])).toEqual([0, 0, 1, 1]);
    const [x, y] = projectLonLat(-95.37, 29.76);
    expect(layout?.x[0]).toBeCloseTo(x, 3);
    expect(layout?.y[0]).toBeCloseTo(y, 3);
  });

  it('marks batteries outside every city block, and fills in missing names from the defaults', () => {
    const layout = readLayout({ ...raw, cities: [{ location: 'LZ_AEN', batteries: 1, first_battery: 1 }] });
    expect(Array.from(layout?.cityOf ?? [])).toEqual([NO_CITY, 0, NO_CITY, NO_CITY]);
    expect(layout?.cities[0]).toMatchObject({ name: 'Austin', short: 'ATX', center: [-97.74, 30.27] });
  });

  it('returns null for anything unusable', () => {
    expect(readLayout(null)).toBeNull();
    expect(readLayout({ ...raw, positions: 42 })).toBeNull();
    expect(readLayout({ ...raw, fleet_size: 10 })).toBeNull();
    expect(readLayout({ ...raw, fleet_size: -1 })).toBeNull();
  });
});
