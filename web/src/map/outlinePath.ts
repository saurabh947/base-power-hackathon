// The Texas outline as SVG path data, for drawing Texas outside the live map's
// canvas (the Start page's faint backdrop). Same outline (texasOutline.ts) and
// projection (EPSG:3083, albers.ts) as the map, in a "frame" of kilometres from
// the outline's north-west corner with y pointing down, so it drops straight
// into <svg viewBox={`0 0 ${OUTLINE_FRAME.width} ${OUTLINE_FRAME.height}`}>.

import { projectLonLat } from './albers.ts';
import { TEXAS_BBOX, TEXAS_OUTLINE } from './texasOutline.ts';

const KM = 1000;

/** The frame's size in kilometres (the outline's bounding box). */
export const OUTLINE_FRAME = Object.freeze({
  width: (TEXAS_BBOX.maxX - TEXAS_BBOX.minX) / KM,
  height: (TEXAS_BBOX.maxY - TEXAS_BBOX.minY) / KM,
});

/** EPSG:3083 metres to the frame: km east of the outline's west edge, km south of its north edge. */
export function toOutlineFrame(x: number, y: number): [number, number] {
  return [(x - TEXAS_BBOX.minX) / KM, (TEXAS_BBOX.maxY - y) / KM];
}

/** Longitude and latitude in degrees to the frame. */
export function lonLatToOutlineFrame(lon: number, lat: number): [number, number] {
  const [x, y] = projectLonLat(lon, lat);
  return toOutlineFrame(x, y);
}

let ring: readonly number[] | null = null;
let path: string | null = null;

/** The outline in the frame as a flat ring [x0, y0, x1, y1, …], closing point left out. */
export function outlineFrameRing(): readonly number[] {
  if (!ring) {
    const out: number[] = [];
    for (let i = 0; i + 1 < TEXAS_OUTLINE.length; i += 2) out.push(...toOutlineFrame(TEXAS_OUTLINE[i], TEXAS_OUTLINE[i + 1]));
    ring = Object.freeze(out);
  }
  return ring;
}

const round = (v: number) => String(Math.round(v * 10) / 10);

/** SVG path data for the outline in the frame, to 0.1 km: "M x y L x y … Z". */
export function outlinePathData(): string {
  if (!path) {
    const r = outlineFrameRing();
    const parts: string[] = [];
    for (let i = 0; i + 1 < r.length; i += 2) parts.push(`${i === 0 ? 'M' : 'L'}${round(r[i])} ${round(r[i + 1])}`);
    path = `${parts.join('')}Z`;
  }
  return path;
}
