// Pure data shaping for the live charts: ticks and forecasts in, uPlot's
// column arrays out. Kept free of DOM code so it can be unit tested.

import type { Forecast, LiveEvent, Tick, TickAction } from '../../api/types.ts';
import { controllerFromDetail } from '../../lib/fleet.ts';

export const TICK_SECONDS = 300;
export const MIN_SPAN_SECONDS = 6 * 3600;

export type WindowKey = '6h' | '24h' | 'all';
export const WINDOW_SECONDS: Record<WindowKey, number | null> = { '6h': 6 * 3600, '24h': 24 * 3600, all: null };
export const WINDOW_LABELS: Record<WindowKey, string> = { '6h': '6 hours', '24h': '24 hours', all: 'Whole replay' };

type Col = (number | null)[];

const toS = (iso: string) => Date.parse(iso) / 1000;

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

export interface XWindow {
  min: number;
  max: number;
}

/**
 * The visible time range, shared by the price and power charts so they line up:
 * the last `windowSeconds` of the replay (at least 6 hours wide, so a new run
 * draws into empty space) plus any forecast that reaches further ahead.
 */
export function computeXWindow(
  firstS: number | null,
  lastS: number | null,
  forecastEndS: number | null,
  windowSeconds: number | null,
): XWindow | null {
  const first = firstS ?? lastS;
  if (first === null) return null;
  const now = lastS ?? first;
  const min = windowSeconds === null ? first : Math.max(first, now - windowSeconds);
  const span = Math.max(MIN_SPAN_SECONDS, windowSeconds ?? now - first);
  const max = Math.max(min + span, now + TICK_SECONDS, forecastEndS ?? -Infinity);
  return { min, max };
}

/** The last forecast point across all areas, if it lies ahead of the replay. */
export function forecastEnd(forecasts: Record<string, Forecast>): number | null {
  let end: number | null = null;
  for (const f of Object.values(forecasts)) {
    for (const p of f.points ?? []) {
      const s = toS(p.ts_utc);
      if (Number.isFinite(s) && (end === null || s > end)) end = s;
    }
  }
  return end;
}

export interface PriceModel {
  /** [x seconds, price, forecast p90, forecast p10, forecast p50, decision price] */
  data: [number[], Col, Col, Col, Col, Col];
  actions: (TickAction | null)[];
  hasForecast: boolean;
}

/** Merge 5-minute ticks with the (15-minute) forecast points on one sorted x axis. */
export function buildPriceModel(history: Tick[], forecast: Forecast | undefined, area: string): PriceModel {
  const points = (forecast?.points ?? [])
    .map((p) => ({ x: toS(p.ts_utc), p }))
    .filter((q) => Number.isFinite(q.x))
    .toSorted((a, b) => a.x - b.x);
  const xs: number[] = [];
  const price: Col = [];
  const p90: Col = [];
  const p10: Col = [];
  const p50: Col = [];
  const decision: Col = [];
  const actions: (TickAction | null)[] = [];

  let i = 0;
  let j = 0;
  while (i < history.length || j < points.length) {
    const tx = i < history.length ? toS(history[i].ts_utc) : Infinity;
    const fx = j < points.length ? points[j].x : Infinity;
    const x = Math.min(tx, fx);
    xs.push(x);
    if (tx === x) {
      const t = history[i++];
      price.push(num(t.price?.[area]));
      decision.push(num(t.decision_price?.[area]));
      actions.push(t.action?.[area] ?? null);
    } else {
      price.push(null);
      decision.push(null);
      actions.push(null);
    }
    if (fx === x) {
      const { p } = points[j++];
      p90.push(num(p.p90));
      p10.push(num(p.p10));
      p50.push(num(p.p50));
    } else {
      p90.push(null);
      p10.push(null);
      p50.push(null);
    }
  }
  return { data: [xs, price, p90, p10, p50, decision], actions, hasForecast: points.length > 0 };
}

export interface ActionSpan {
  start: number;
  end: number;
  action: 'charge' | 'sell';
}

/** Runs of charging or selling; each tick covers [ts, ts + 5 min). Gaps in the data split runs. */
export function actionSpans(xs: number[], actions: (TickAction | null)[]): ActionSpan[] {
  const spans: ActionSpan[] = [];
  let current: ActionSpan | null = null;
  for (let k = 0; k < xs.length; k++) {
    const a = actions[k];
    if (a === null || a === undefined) continue; // forecast-only point
    if (a === 'charge' || a === 'sell') {
      if (current && current.action === a && xs[k] <= current.end) {
        current.end = xs[k] + TICK_SECONDS;
      } else {
        current = { start: xs[k], end: xs[k] + TICK_SECONDS, action: a };
        spans.push(current);
      }
    } else {
      current = null;
    }
  }
  return spans;
}

function fleetValue(byLoc: Partial<Record<string, number>> | undefined): number | null {
  if (!byLoc) return null;
  const fleet = num(byLoc.fleet);
  if (fleet !== null) return fleet;
  let sum = 0;
  let any = false;
  for (const [k, v] of Object.entries(byLoc)) {
    if (k === 'fleet') continue;
    const n = num(v);
    if (n !== null) {
      sum += n;
      any = true;
    }
  }
  return any ? sum : null;
}

export interface PowerModel {
  /** [x seconds, asked-for MW, delivered MW] */
  data: [number[], Col, Col];
}

export function buildPowerModel(history: Tick[]): PowerModel {
  const xs: number[] = [];
  const plan: Col = [];
  const delivered: Col = [];
  for (const t of history) {
    xs.push(toS(t.ts_utc));
    const target = fleetValue(t.target_kw);
    const actual = fleetValue(t.actual_kw);
    plan.push(target === null ? null : target / 1000);
    delivered.push(actual === null ? null : actual / 1000);
  }
  return { data: [xs, plan, delivered] };
}

export interface CrashMarker {
  x: number;
  label: string;
}

/** Controller crashes to mark on the power chart, from the event feed. */
export function crashMarkers(events: LiveEvent[]): CrashMarker[] {
  const out: CrashMarker[] = [];
  for (const e of events) {
    if (e.kind !== 'worker_dead' || !e.sim_utc) continue;
    const x = toS(e.sim_utc);
    if (!Number.isFinite(x)) continue;
    const controller = controllerFromDetail(e.detail);
    out.push({ x, label: controller === null ? 'Crash' : `Controller ${controller}` });
  }
  return out;
}
