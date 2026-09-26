// Plane geometry for the map: rings are flat arrays [x0, y0, x1, y1, …] with
// the closing point left out. Used by the outline generator
// (scripts/texas-outline.ts) and by the map at run time.

export type Ring = ArrayLike<number>;

export interface BBox {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export function ringBBox(ring: Ring): BBox {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (let i = 0; i + 1 < ring.length; i += 2) {
    const x = ring[i];
    const y = ring[i + 1];
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  return { minX, minY, maxX, maxY };
}

/** Signed area (shoelace); positive when the ring runs counter-clockwise with y up. */
export function ringArea(ring: Ring): number {
  const n = ring.length >> 1;
  let sum = 0;
  for (let i = 0, j = n - 1; i < n; j = i++) {
    sum += ring[2 * j] * ring[2 * i + 1] - ring[2 * i] * ring[2 * j + 1];
  }
  return sum / 2;
}

/** Even-odd rule: is (x, y) inside the ring? */
export function pointInRing(x: number, y: number, ring: Ring): boolean {
  const n = ring.length >> 1;
  let inside = false;
  for (let i = 0, j = n - 1; i < n; j = i++) {
    const xi = ring[2 * i];
    const yi = ring[2 * i + 1];
    const xj = ring[2 * j];
    const yj = ring[2 * j + 1];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function segmentDistanceSq(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax;
  const dy = by - ay;
  const len = dx * dx + dy * dy;
  let t = len === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / len;
  t = Math.max(0, Math.min(1, t));
  const ex = ax + t * dx - px;
  const ey = ay + t * dy - py;
  return ex * ex + ey * ey;
}

/**
 * Douglas–Peucker on an open polyline of `count` points starting at point `first`
 * of `points` (flat [x, y, …]). Marks kept points in `keep`; iterative, so long
 * lines cannot overflow the stack.
 */
function markDouglasPeucker(points: Ring, first: number, last: number, toleranceSq: number, keep: Uint8Array): void {
  keep[first] = 1;
  keep[last] = 1;
  const stack: [number, number][] = [[first, last]];
  while (stack.length) {
    const [a, b] = stack.pop() as [number, number];
    let maxD = -1;
    let index = -1;
    const ax = points[2 * a];
    const ay = points[2 * a + 1];
    const bx = points[2 * b];
    const by = points[2 * b + 1];
    for (let i = a + 1; i < b; i++) {
      const d = segmentDistanceSq(points[2 * i], points[2 * i + 1], ax, ay, bx, by);
      if (d > maxD) {
        maxD = d;
        index = i;
      }
    }
    if (index !== -1 && maxD > toleranceSq) {
      keep[index] = 1;
      stack.push([a, index], [index, b]);
    }
  }
}

/** Douglas–Peucker simplification of an open polyline. */
export function simplifyLine(points: Ring, tolerance: number): number[] {
  const n = points.length >> 1;
  if (n <= 2) return Array.from(points);
  const keep = new Uint8Array(n);
  markDouglasPeucker(points, 0, n - 1, tolerance * tolerance, keep);
  const out: number[] = [];
  for (let i = 0; i < n; i++) if (keep[i]) out.push(points[2 * i], points[2 * i + 1]);
  return out;
}

/**
 * Douglas–Peucker for a closed ring: split at the first point and the point
 * farthest from it, simplify both halves, and join them.
 */
export function simplifyRing(ring: Ring, tolerance: number): number[] {
  const n = ring.length >> 1;
  if (n <= 4) return Array.from(ring);
  let far = 1;
  let farD = -1;
  for (let i = 1; i < n; i++) {
    const d = (ring[2 * i] - ring[0]) ** 2 + (ring[2 * i + 1] - ring[1]) ** 2;
    if (d > farD) {
      farD = d;
      far = i;
    }
  }
  // Walk 0 → far → back to 0 (as index n) so both halves are open polylines.
  const closed = new Float64Array(2 * (n + 1));
  closed.set(ring as ArrayLike<number>);
  closed[2 * n] = ring[0];
  closed[2 * n + 1] = ring[1];
  const keep = new Uint8Array(n + 1);
  const tolSq = tolerance * tolerance;
  markDouglasPeucker(closed, 0, far, tolSq, keep);
  markDouglasPeucker(closed, far, n, tolSq, keep);
  const out: number[] = [];
  for (let i = 0; i < n; i++) if (keep[i]) out.push(ring[2 * i], ring[2 * i + 1]);
  return out;
}

function orient(ax: number, ay: number, bx: number, by: number, cx: number, cy: number): number {
  const v = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
  return v > 0 ? 1 : v < 0 ? -1 : 0;
}

/** True if any two non-adjacent edges of the ring cross (a simplification must never do that). */
export function ringSelfIntersects(ring: Ring): boolean {
  const n = ring.length >> 1;
  for (let i = 0; i < n; i++) {
    const i2 = (i + 1) % n;
    for (let j = i + 1; j < n; j++) {
      const j2 = (j + 1) % n;
      if (j === i2 || j2 === i) continue; // adjacent edges share a point
      const o1 = orient(ring[2 * i], ring[2 * i + 1], ring[2 * i2], ring[2 * i2 + 1], ring[2 * j], ring[2 * j + 1]);
      const o2 = orient(ring[2 * i], ring[2 * i + 1], ring[2 * i2], ring[2 * i2 + 1], ring[2 * j2], ring[2 * j2 + 1]);
      const o3 = orient(ring[2 * j], ring[2 * j + 1], ring[2 * j2], ring[2 * j2 + 1], ring[2 * i], ring[2 * i + 1]);
      const o4 = orient(ring[2 * j], ring[2 * j + 1], ring[2 * j2], ring[2 * j2 + 1], ring[2 * i2], ring[2 * i2 + 1]);
      if (o1 !== o2 && o3 !== o4) return true;
    }
  }
  return false;
}

/** Maps projected metres (y up) to screen pixels (y down), keeping the aspect ratio. */
export interface Viewport {
  scale: number;
  offsetX: number;
  offsetY: number;
  /** Screen box the map occupies. */
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface Padding {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

/** The largest uniform scale that fits `bbox` inside `width` × `height` minus `padding`, centred. */
export function fitBBox(bbox: BBox, width: number, height: number, padding: Padding): Viewport {
  const availW = Math.max(1, width - padding.left - padding.right);
  const availH = Math.max(1, height - padding.top - padding.bottom);
  const bw = Math.max(1e-9, bbox.maxX - bbox.minX);
  const bh = Math.max(1e-9, bbox.maxY - bbox.minY);
  const scale = Math.min(availW / bw, availH / bh);
  const w = bw * scale;
  const h = bh * scale;
  const left = padding.left + (availW - w) / 2;
  const top = padding.top + (availH - h) / 2;
  return { scale, offsetX: left - bbox.minX * scale, offsetY: top + bbox.maxY * scale, left, top, width: w, height: h };
}

export function toScreenX(v: Viewport, x: number): number {
  return v.offsetX + x * v.scale;
}

export function toScreenY(v: Viewport, y: number): number {
  return v.offsetY - y * v.scale;
}
