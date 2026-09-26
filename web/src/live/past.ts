// The live view as it was at an earlier moment of the run, for the timeline (docs/UX.md, "Live map v2").
// The corners work from the tick history for any moment it holds: the clock, the prices, the power, and the
// money (today's totals minus what the later ticks earned). The controllers (with their charge), the
// availability and the race come from the frames this page recorded (src/live/recorder.ts); before the page
// was opened, the controllers are rebuilt from the worker events, their charge is unknown, and so are the
// availability and the race. Pure, so it is unit tested.

import type { ByLocation, LiveEvent, LocationTotals, Tick, Totals, Worker, WorkerStatus } from '../api/types.ts';
import { applyAdoption, applyRecovery, defaultOwners } from '../map/owners.ts';
import type { FrameRecord } from './recorder.ts';
import type { LiveState } from './reducer.ts';

/** A tick is 5 minutes: kW × this is kWh. */
const TICK_H = 5 / 60;

function finite(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

export function tickMs(t: Tick): number {
  return Date.parse(t.ts_utc);
}

/** Index of the last tick at or before `tMs`, or -1 when `tMs` is before the first one. History is sorted. */
export function indexAt(history: readonly Tick[], tMs: number): number {
  let lo = 0;
  let hi = history.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (tickMs(history[mid]) <= tMs) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

/**
 * The moment `steps` ticks away from `tMs` (null = live, the latest tick). Stepping forward onto the latest tick
 * goes back to live (null); stepping back stops at the first tick.
 */
export function stepFrom(history: readonly Tick[], tMs: number | null, steps: number): number | null {
  if (!history.length) return null;
  const last = history.length - 1;
  const from = tMs === null ? last : Math.max(0, indexAt(history, tMs));
  const to = Math.max(0, from + steps);
  if (to >= last) return steps > 0 || tMs === null ? null : tickMs(history[last]);
  return tickMs(history[to]);
}

/** A tick value for the whole fleet: `fleet`, else the sum of the cities. */
export function fleetValue(v: ByLocation<number> | undefined): number {
  if (!v) return 0;
  if (finite(v.fleet)) return v.fleet;
  let sum = 0;
  for (const [k, n] of Object.entries(v)) if (k !== 'fleet' && finite(n)) sum += n;
  return sum;
}

/**
 * The totals as they were at history[index]: today's totals minus what every later tick added. Energy per
 * city and tick is actual_kw × 5 minutes, sold when positive and bought when negative, as the simulator
 * meters it.
 */
export function totalsAt(totals: Totals, history: readonly Tick[], index: number): Totals {
  if (index >= history.length - 1) return totals;
  let revenue = 0;
  let sold = 0;
  let bought = 0;
  const byLoc: Record<string, LocationTotals> = {};
  for (const [loc, v] of Object.entries(totals.by_location ?? {})) byLoc[loc] = { ...v };
  for (let k = Math.max(0, index + 1); k < history.length; k++) {
    const t = history[k];
    revenue += fleetValue(t.revenue_usd);
    for (const [loc, kw] of Object.entries(t.actual_kw ?? {})) {
      if (loc === 'fleet' || !finite(kw)) continue;
      const kwh = kw * TICK_H;
      if (kwh > 0) sold += kwh;
      else bought -= kwh;
      const b = byLoc[loc];
      if (!b) continue;
      const r = t.revenue_usd?.[loc];
      if (finite(r)) b.revenue_usd -= r;
      if (kwh > 0) b.sold_kwh = Math.max(0, b.sold_kwh - kwh);
      else b.bought_kwh = Math.max(0, b.bought_kwh + kwh);
    }
  }
  const out: Totals = {
    revenue_usd: totals.revenue_usd - revenue,
    sold_kwh: Math.max(0, totals.sold_kwh - sold),
    bought_kwh: Math.max(0, totals.bought_kwh - bought),
    ticks: Math.max(0, totals.ticks - (history.length - 1 - index)),
  };
  if (totals.by_location) out.by_location = byLoc;
  return out;
}

/** Where the batteries are, to count each controller's batteries per city. */
export interface FleetCities {
  size: number;
  /** City index of every battery (src/map/layout.ts cityOf). */
  cityOf: Uint8Array;
  /** Location ids by city index. */
  locations: readonly string[];
}

function oldestFirst(events: readonly LiveEvent[]): LiveEvent[] {
  return events.toReversed(); // LiveState.events is newest first
}

function eventMs(e: LiveEvent): number | null {
  const t = e.sim_utc ? Date.parse(e.sim_utc) : Number.NaN;
  return Number.isFinite(t) ? t : null;
}

/**
 * The controllers as they were at `tMs`, rebuilt from the worker events up to then: not responding after
 * worker_suspect, crashed after worker_dead, their batteries handed out round-robin after worker_adopted
 * (src/map/owners.ts, like the backend), and back on their own block after a restart (worker_recovered). With
 * `cities`, each controller's batteries are counted per city. How charged they were is not in the events, so
 * charge_level and usable_charge are left out (unknown).
 */
export function workersAt(template: readonly Worker[], events: readonly LiveEvent[], tMs: number, cities?: FleetCities): Worker[] {
  const n = template.length || 20;
  const status = new Map<number, WorkerStatus>(template.map((w) => [w.worker_id, 'healthy']));
  const adoptedFrom = new Map<number, number[]>(template.map((w) => [w.worker_id, []]));
  const owners = cities ? defaultOwners(cities.size, n) : null;
  for (const e of oldestFirst(events)) {
    const at = eventMs(e);
    if (at === null || at > tMs) continue;
    const w = e.detail?.worker_id;
    if (typeof w !== 'number' || !status.has(w)) continue;
    if (e.kind === 'worker_suspect' && status.get(w) !== 'dead') status.set(w, 'suspect');
    else if (e.kind === 'worker_dead') status.set(w, 'dead');
    else if (e.kind === 'worker_adopted') {
      status.set(w, 'dead');
      const by = Array.isArray(e.detail?.adopted_by) ? e.detail.adopted_by.filter((x): x is number => typeof x === 'number') : [];
      for (const a of by) adoptedFrom.get(a)?.push(w);
      if (owners) applyAdoption(owners, w, by);
    } else if (e.kind === 'worker_recovered') {
      status.set(w, 'healthy');
      for (const list of adoptedFrom.values()) {
        const k = list.indexOf(w);
        if (k >= 0) list.splice(k, 1);
      }
      if (owners) applyRecovery(owners, w, n);
    }
  }
  let counts: Map<number, { total: number; byCity: number[] }> | null = null;
  if (owners && cities) {
    counts = new Map(template.map((w) => [w.worker_id, { total: 0, byCity: cities.locations.map(() => 0) }]));
    for (let g = 0; g < owners.length; g++) {
      const c = counts.get(owners[g]);
      if (!c) continue;
      c.total++;
      const k = cities.cityOf[g];
      if (k < c.byCity.length) c.byCity[k]++;
    }
  }
  return template.map((w) => {
    const s = status.get(w.worker_id) ?? 'healthy';
    const c = counts?.get(w.worker_id);
    let byLocation = w.by_location;
    if (c && cities) {
      byLocation = {};
      cities.locations.forEach((loc, k) => {
        if (c.byCity[k]) (byLocation as Record<string, number>)[loc] = c.byCity[k];
      });
    }
    return {
      ...w,
      status: s,
      batteries: c ? c.total : s === 'dead' ? 0 : w.batteries,
      adopted_from: adoptedFrom.get(w.worker_id) ?? [],
      last_tick_utc: null,
      by_location: byLocation,
      charge_level: undefined,
      usable_charge: undefined,
    };
  });
}

/**
 * The live state as it was at `tMs`: history up to that tick (so "the latest tick" is that one), the money
 * then, the events and forecasts up to then, and the recorded controllers, availability and race (null race
 * and availability before the page was opened). The run's own facts (status, speed, settings) stay live.
 */
export function pastState(live: LiveState, tMs: number, record: FrameRecord | null, cities?: FleetCities): LiveState {
  const index = Math.max(0, indexAt(live.history, tMs));
  const tick = live.history[index];
  if (!tick) return live;
  const t = tickMs(tick);
  const forecast: LiveState['forecast'] = {};
  for (const [loc, f] of Object.entries(live.forecast)) {
    if (Date.parse(f.issued_utc) <= t) forecast[loc] = f;
  }
  return {
    ...live,
    simTimeUtc: tick.ts_utc,
    history: live.history.slice(0, index + 1),
    totals: totalsAt(live.totals, live.history, index),
    workers: record ? record.workers : workersAt(live.workers, live.events, t, cities),
    availability: record ? record.availability : null,
    events: live.events.filter((e) => {
      const at = eventMs(e);
      return at !== null && at <= t;
    }),
    forecast,
    race: record ? record.race : null,
    batteryStatus: null,
  };
}

/**
 * Money earned on predicted ticks (docs/API.md Tick.predicted: a projection), for all of Texas (null) or one
 * city; null when no tick so far is predicted. `exact` is false when the kept history starts inside the
 * prediction, so earlier predicted ticks are not in it.
 */
export function projectedMoney(history: readonly Tick[], scope: string | null): { usd: number; exact: boolean } | null {
  let usd = 0;
  let any = false;
  for (const t of history) {
    if (!t.predicted) continue;
    any = true;
    if (scope === null) usd += fleetValue(t.revenue_usd);
    else {
      const r = t.revenue_usd?.[scope];
      if (finite(r)) usd += r;
    }
  }
  return any ? { usd, exact: !history[0]?.predicted } : null;
}
