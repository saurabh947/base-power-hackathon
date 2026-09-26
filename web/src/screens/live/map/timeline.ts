// The timeline scrubber's track (docs/UX.md, "Live map v2"): a mini price line for the run so far, green
// bands where the fleet sold and blue bands where it bought, ▲/▼ at each day's high and low, and the part
// where the prices are the AI's prediction shaded. Pure, so it is unit tested; Timeline.tsx draws it.
//
// Each 5-minute tick owns a slot [ts, ts + 5 min) on the track, so the latest tick is visible too; its price
// point and the scrubber's thumb sit in the middle of the slot.

import type { Tick } from '../../../api/types.ts';
import { fleetValue, indexAt, tickMs } from '../../../live/past.ts';
import { fmtDate, fmtTimeOfDay, localDateOf } from '../../../lib/format.ts';

export const TICK_MS = 5 * 60_000;

/** "May 31" */
export function shortDate(tMs: number): string {
  return fmtDate(tMs).replace(/, \d{4}$/, '');
}

/** "7:45 pm" on the replay's first day, "7:45 pm, May 31" after it. */
export function momentLabel(tMs: number, firstMs: number | null): string {
  const time = fmtTimeOfDay(tMs);
  if (firstMs === null || localDateOf(tMs) === localDateOf(firstMs)) return time;
  return `${time}, ${shortDate(tMs)}`;
}

export interface TrackBand {
  x0: number;
  x1: number;
  kind: 'sell' | 'buy';
}

export interface TrackMarker {
  x: number;
  y: number;
  kind: 'high' | 'low';
  price: number;
  index: number;
}

export interface Track {
  width: number;
  height: number;
  /** Start of the first tick's slot and end of the last one's, ms. */
  t0: number;
  t1: number;
  count: number;
  /** SVG path of the price line. */
  path: string;
  bands: TrackBand[];
  markers: TrackMarker[];
  /** Where the AI's prediction takes over, px (null: every price so far is real). */
  predictedX: number | null;
}

function finite(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

/**
 * The price the track follows: the selected city's, or for all of Texas the average of the cities' prices
 * weighted by their batteries (`weights`, by location id; equal weights when missing).
 */
export function trackPrice(t: Tick, scope: string | null, weights: Record<string, number>): number | null {
  if (scope !== null) {
    const p = t.price?.[scope];
    return finite(p) ? p : null;
  }
  let sum = 0;
  let w = 0;
  for (const [loc, p] of Object.entries(t.price ?? {})) {
    if (loc === 'fleet' || !finite(p)) continue;
    const k = weights[loc] ?? 1;
    sum += p * k;
    w += k;
  }
  return w > 0 ? sum / w : null;
}

/** Selling or buying at this tick: the fleet's (or the city's) delivered power, else its action. */
export function trackAction(t: Tick, scope: string | null): 'sell' | 'buy' | null {
  const kw = scope === null ? (t.actual_kw ? fleetValue(t.actual_kw) : null) : t.actual_kw?.[scope];
  if (finite(kw) && Math.abs(kw) >= 1) return kw > 0 ? 'sell' : 'buy';
  if (finite(kw)) return null;
  const actions = scope === null ? Object.values(t.action ?? {}) : [t.action?.[scope]];
  const sells = actions.filter((a) => a === 'sell').length;
  const buys = actions.filter((a) => a === 'charge').length;
  if (sells > buys) return 'sell';
  if (buys > sells) return 'buy';
  return null;
}

/** Prices are drawn on an asinh scale: linear near zero, compressed for spikes, so an ordinary day still shows. */
export function priceScale(p: number): number {
  return Math.asinh(p / 40);
}

export function timeToX(track: Pick<Track, 't0' | 't1' | 'width'>, tMs: number): number {
  const span = Math.max(1, track.t1 - track.t0);
  return Math.min(track.width, Math.max(0, ((tMs - track.t0) / span) * track.width));
}

/** Where tick `index` sits on the track: the middle of its slot. */
export function indexToX(track: Pick<Track, 't0' | 't1' | 'width'>, history: readonly Tick[], index: number): number {
  const t = history[Math.min(history.length - 1, Math.max(0, index))];
  return t ? timeToX(track, tickMs(t) + TICK_MS / 2) : 0;
}

/** The tick whose slot is under `x` (clamped to the run so far). */
export function xToIndex(track: Pick<Track, 't0' | 't1' | 'width'>, history: readonly Tick[], x: number): number {
  if (!history.length) return -1;
  const tMs = track.t0 + (Math.min(track.width, Math.max(0, x)) / Math.max(1, track.width)) * (track.t1 - track.t0);
  return Math.min(history.length - 1, Math.max(0, indexAt(history, tMs)));
}

export function buildTrack(
  history: readonly Tick[],
  scope: string | null,
  weights: Record<string, number>,
  width: number,
  height: number,
): Track | null {
  const n = history.length;
  if (!n || width <= 0 || height <= 0) return null;
  const t0 = tickMs(history[0]);
  const t1 = tickMs(history[n - 1]) + TICK_MS;
  const geom = { t0, t1, width };
  const prices = history.map((t) => trackPrice(t, scope, weights));

  // Vertical scale: the whole run's range, with room for the markers above and below.
  let lo = Infinity;
  let hi = -Infinity;
  for (const p of prices) {
    if (p === null) continue;
    const v = priceScale(p);
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  if (!Number.isFinite(lo)) {
    lo = 0;
    hi = 1;
  }
  lo = Math.min(lo, 0);
  if (hi - lo < 0.5) hi = lo + 0.5;
  const top = 7;
  const bottom = height - 7;
  const yOf = (p: number) => bottom - ((priceScale(p) - lo) / (hi - lo)) * (bottom - top);

  // The line: every tick while they fit, else each pixel column's first, lowest, highest and last (M4).
  const points: [number, number][] = [];
  if (n <= width * 2) {
    prices.forEach((p, i) => {
      if (p !== null) points.push([indexToX(geom, history, i), yOf(p)]);
    });
  } else {
    let col = -1;
    let bucket: { x: number; i: number; p: number }[] = [];
    const flush = () => {
      if (!bucket.length) return;
      const first = bucket[0];
      const last = bucket[bucket.length - 1];
      const min = bucket.reduce((a, b) => (b.p < a.p ? b : a));
      const max = bucket.reduce((a, b) => (b.p > a.p ? b : a));
      for (const q of [first, min, max, last].toSorted((a, b) => a.i - b.i)) points.push([q.x, yOf(q.p)]);
      bucket = [];
    };
    prices.forEach((p, i) => {
      if (p === null) return;
      const x = indexToX(geom, history, i);
      if (Math.floor(x) !== col) {
        flush();
        col = Math.floor(x);
      }
      bucket.push({ x, i, p });
    });
    flush();
  }
  const path = points.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join('');

  // Bands: runs of selling or buying, each tick its slot.
  const bands: TrackBand[] = [];
  let run: TrackBand | null = null;
  history.forEach((t, i) => {
    const kind = trackAction(t, scope);
    const x0 = timeToX(geom, tickMs(t));
    const x1 = timeToX(geom, tickMs(t) + TICK_MS);
    if (kind && run && run.kind === kind && i > 0 && tickMs(t) - tickMs(history[i - 1]) <= TICK_MS) {
      run.x1 = x1;
    } else if (kind) {
      run = { x0, x1, kind };
      bands.push(run);
    } else {
      run = null;
    }
  });

  // ▲ each (Central-time) day's highest price, ▼ its lowest, never two of a kind on top of each other.
  const days = new Map<string, { hi: number; lo: number }>();
  prices.forEach((p, i) => {
    if (p === null) return;
    const t = history[i];
    const day = (typeof t.ts_local === 'string' && /^\d{4}-\d{2}-\d{2}/.test(t.ts_local) ? t.ts_local.slice(0, 10) : null) ?? localDateOf(t.ts_utc) ?? '';
    const d = days.get(day);
    if (!d) days.set(day, { hi: i, lo: i });
    else {
      if (p > (prices[d.hi] as number)) d.hi = i;
      if (p < (prices[d.lo] as number)) d.lo = i;
    }
  });
  const markers: TrackMarker[] = [];
  for (const d of days.values()) {
    for (const [kind, i] of [
      ['high', d.hi],
      ['low', d.lo],
    ] as const) {
      const p = prices[i] as number;
      const x = indexToX(geom, history, i);
      if (d.hi === d.lo) continue;
      if (markers.some((m) => m.kind === kind && Math.abs(m.x - x) < 10)) continue;
      markers.push({ x, y: yOf(p), kind, price: p, index: i });
    }
  }

  const firstPredicted = history.findIndex((t) => t.predicted);
  return {
    width,
    height,
    t0,
    t1,
    count: n,
    path,
    bands,
    markers,
    predictedX: firstPredicted >= 0 ? timeToX(geom, tickMs(history[firstPredicted])) : null,
  };
}
