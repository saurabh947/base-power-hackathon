// The numbers the map's corners show, for all of Texas (scope null) or one city
// (a location id): per-location tick values, totals.by_location,
// availability.by_location and workers[].by_location (docs/API.md).

import type { AreaAvailability, Availability, ByLocation, LocationTotals, Tick, Totals, Worker } from '../../../api/types.ts';

export type Scope = string | null;

function finite(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

/** A tick value for the scope: the city's own, or the fleet's (`fleet`, else the sum of the areas). */
export function scopeValue(v: ByLocation<number> | undefined, scope: Scope): number | null {
  if (!v) return null;
  if (scope !== null) return finite(v[scope]) ? (v[scope] as number) : null;
  if (finite(v.fleet)) return v.fleet;
  let sum = 0;
  let any = false;
  for (const [k, n] of Object.entries(v)) {
    if (k !== 'fleet' && finite(n)) {
      sum += n;
      any = true;
    }
  }
  return any ? sum : null;
}

/** Money and energy so far for the scope; null when the server sent no per-city totals. */
export function scopeTotals(totals: Totals, scope: Scope): LocationTotals | null {
  if (scope === null) return totals;
  const t = totals.by_location?.[scope];
  return t && finite(t.revenue_usd) ? t : null;
}

export function scopeAvailability(a: Availability | null, scope: Scope): AreaAvailability | null {
  if (!a) return null;
  if (scope === null) return { available: a.available, total: a.total };
  return a.by_location?.[scope] ?? null;
}

/** How many of the scope's batteries each controller runs (all of them for all of Texas). */
export function workerShare(w: Worker, scope: Scope): number {
  if (scope === null) return w.batteries;
  const n = w.by_location?.[scope];
  return finite(n) ? n : 0;
}

/** 5% is the spec's tracking target. */
export const ON_TARGET_PCT = 5;

/**
 * The same measure as the tick's error_pct, |actual − target| / max(|target|, 0.5% of nameplate),
 * for one city: its nameplate is estimated from its battery count and the average power.
 */
export function trackingErrorPct(target: number, actual: number, nameplateKw: number): number {
  return (Math.abs(actual - target) / Math.max(Math.abs(target), 0.005 * nameplateKw, 1e-9)) * 100;
}

export interface Tracking {
  target: number | null;
  actual: number | null;
  errorPct: number | null;
}

export function scopeTracking(tick: Tick | null, scope: Scope, batteries: number, avgPowerKw: number): Tracking {
  if (!tick) return { target: null, actual: null, errorPct: null };
  const target = scopeValue(tick.target_kw, scope);
  const actual = scopeValue(tick.actual_kw, scope);
  let errorPct: number | null = null;
  if (scope === null && finite(tick.error_pct)) errorPct = tick.error_pct;
  else if (target !== null && actual !== null) errorPct = trackingErrorPct(target, actual, batteries * avgPowerKw);
  return { target, actual, errorPct };
}
