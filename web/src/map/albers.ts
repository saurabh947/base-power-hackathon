// Albers equal-area conic projection on an ellipsoid (Snyder, "Map Projections:
// A Working Manual", USGS Professional Paper 1395, 1987, pp. 98-103), written
// out here so the Texas outline (scripts/texas-outline.ts) and the battery dots
// are projected by the very same function.
//
// The map uses EPSG:3083, NAD83 / Texas Centric Albers Equal Area: GRS 1980
// ellipsoid, standard parallels 27°30'N and 35°N, latitude of origin 18°N,
// central meridian 100°W, false easting 1,500,000 m, false northing 6,000,000 m.
// Output is in metres, x east and y north.

export interface AlbersParams {
  /** Semi-major axis, metres. */
  a: number;
  /** Inverse flattening. */
  invF: number;
  lat1: number;
  lat2: number;
  lat0: number;
  lon0: number;
  falseEasting: number;
  falseNorthing: number;
}

export interface AlbersProjection {
  readonly params: AlbersParams;
  /** [x, y] in metres from longitude and latitude in degrees. */
  forward(lon: number, lat: number): [number, number];
  /** [lon, lat] in degrees from x and y in metres. */
  inverse(x: number, y: number): [number, number];
  /** Scale factor along the parallel at `lat` (1 on both standard parallels). */
  parallelScale(lat: number): number;
}

const RAD = Math.PI / 180;

export function albersEqualArea(params: AlbersParams): AlbersProjection {
  const { a, invF, lat1, lat2, lat0, lon0, falseEasting, falseNorthing } = params;
  const f = 1 / invF;
  const e2 = 2 * f - f * f;
  const e = Math.sqrt(e2);

  const m = (phi: number) => Math.cos(phi) / Math.sqrt(1 - e2 * Math.sin(phi) ** 2);
  const q = (phi: number) => {
    const s = Math.sin(phi);
    return (1 - e2) * (s / (1 - e2 * s * s) - (1 / (2 * e)) * Math.log((1 - e * s) / (1 + e * s)));
  };

  const phi1 = lat1 * RAD;
  const phi2 = lat2 * RAD;
  const m1 = m(phi1);
  const m2 = m(phi2);
  const q1 = q(phi1);
  const q2 = q(phi2);
  const n = Math.abs(phi1 - phi2) < 1e-12 ? Math.sin(phi1) : (m1 * m1 - m2 * m2) / (q2 - q1);
  const C = m1 * m1 + n * q1;
  const rho = (qq: number) => (a * Math.sqrt(Math.max(0, C - n * qq))) / n;
  const rho0 = rho(q(lat0 * RAD));

  return {
    params,
    forward(lon, lat) {
      const r = rho(q(lat * RAD));
      const theta = n * (lon - lon0) * RAD;
      return [falseEasting + r * Math.sin(theta), falseNorthing + rho0 - r * Math.cos(theta)];
    },
    inverse(x, y) {
      const dx = x - falseEasting;
      const dy = rho0 - (y - falseNorthing);
      const sign = n < 0 ? -1 : 1;
      const r = Math.hypot(dx, dy);
      const theta = Math.atan2(sign * dx, sign * dy);
      const qq = (C - (r * r * n * n) / (a * a)) / n;
      // Snyder eq. 3-16, iterated to convergence.
      let phi = Math.asin(Math.max(-1, Math.min(1, qq / 2)));
      for (let i = 0; i < 15; i++) {
        const s = Math.sin(phi);
        const w = 1 - e2 * s * s;
        const step =
          ((w * w) / (2 * Math.cos(phi))) *
          (qq / (1 - e2) - s / w + (1 / (2 * e)) * Math.log((1 - e * s) / (1 + e * s)));
        phi += step;
        if (Math.abs(step) < 1e-13) break;
      }
      return [lon0 + theta / n / RAD, phi / RAD];
    },
    parallelScale(lat) {
      const phi = lat * RAD;
      return (rho(q(phi)) * n) / (a * m(phi));
    },
  };
}

/** EPSG:3083, NAD83 / Texas Centric Albers Equal Area. */
export const TEXAS_ALBERS_PARAMS: AlbersParams = Object.freeze({
  a: 6_378_137,
  invF: 298.257222101,
  lat1: 27.5,
  lat2: 35,
  lat0: 18,
  lon0: -100,
  falseEasting: 1_500_000,
  falseNorthing: 6_000_000,
});

export const texasAlbers: AlbersProjection = albersEqualArea(TEXAS_ALBERS_PARAMS);

/** EPSG:3083 metres [x, y] for a longitude and latitude in degrees. */
export function projectLonLat(lon: number, lat: number): [number, number] {
  return texasAlbers.forward(lon, lat);
}
