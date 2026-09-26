import { describe, expect, it } from 'vitest';
import type { LiveEvent, Tick, Totals, Worker } from '../api/types.ts';
import { indexAt, pastState, projectedMoney, stepFrom, tickMs, totalsAt, workersAt } from './past.ts';
import type { FrameRecord } from './recorder.ts';
import { initialLiveState, type LiveState } from './reducer.ts';

const T0 = Date.parse('2025-07-11T23:00:00Z');
const iso = (ms: number) => new Date(ms).toISOString().replace('.000Z', 'Z');

/** Tick i: Houston sells 1,200 kW and Dallas–Fort Worth buys 600 kW, at $40 + i. */
function tick(i: number, predicted = false): Tick {
  const ts = iso(T0 + i * 300_000);
  const price = 40 + i;
  return {
    ts_utc: ts,
    ts_local: ts,
    price: { LZ_HOUSTON: price, LZ_NORTH: price },
    action: { LZ_HOUSTON: 'sell', LZ_NORTH: 'charge' },
    target_kw: { LZ_HOUSTON: 1200, LZ_NORTH: -600, fleet: 600 },
    actual_kw: { LZ_HOUSTON: 1200, LZ_NORTH: -600, fleet: 600 },
    error_pct: 0,
    revenue_usd: { LZ_HOUSTON: price * 0.1, LZ_NORTH: -price * 0.05, fleet: price * 0.05 },
    charge_level: { fleet: 0.5 },
    healthy_workers: 20,
    predicted,
  };
}

const history = Array.from({ length: 10 }, (_, i) => tick(i, i >= 7));

const sum = (f: (t: Tick) => number) => history.reduce((s, t) => s + f(t), 0);

/** Totals after all ten ticks, plus $100 earned before the kept history began. */
function totals(): Totals {
  return {
    revenue_usd: 100 + sum((t) => t.revenue_usd.fleet as number),
    sold_kwh: 50 + (1200 * 10) / 12,
    bought_kwh: 20 + (600 * 10) / 12,
    ticks: 30,
    by_location: {
      LZ_HOUSTON: { revenue_usd: 60 + sum((t) => t.revenue_usd.LZ_HOUSTON as number), sold_kwh: 50 + 1000, bought_kwh: 0 },
      LZ_NORTH: { revenue_usd: 40 + sum((t) => t.revenue_usd.LZ_NORTH as number), sold_kwh: 0, bought_kwh: 20 + 500 },
    },
  };
}

const worker = (id: number, cities: Record<string, number>): Worker => ({
  worker_id: id,
  status: 'healthy',
  pid: 100 + id,
  batteries: 5,
  last_tick_utc: null,
  adopted_from: [],
  by_location: cities,
});

function event(kind: string, tickIndex: number, detail: Record<string, unknown>): LiveEvent {
  const at = iso(T0 + tickIndex * 300_000);
  return { id: `${kind}-${tickIndex}`, wall_utc: at, sim_utc: at, kind, severity: 'warn', message: kind, detail };
}

describe('moving along the history', () => {
  it('finds the tick at or before a moment', () => {
    expect(indexAt(history, tickMs(history[3]))).toBe(3);
    expect(indexAt(history, tickMs(history[3]) + 60_000)).toBe(3);
    expect(indexAt(history, T0 - 1)).toBe(-1);
    expect(indexAt(history, T0 + 10 * 3_600_000)).toBe(9);
    expect(indexAt([], T0)).toBe(-1);
  });

  it('steps a tick (or an hour) at a time, and back to live past the latest tick', () => {
    // Live (null): ← goes to the tick before the latest.
    expect(stepFrom(history, null, -1)).toBe(tickMs(history[8]));
    expect(stepFrom(history, null, -12)).toBe(tickMs(history[0])); // stops at the first tick
    expect(stepFrom(history, tickMs(history[4]), 1)).toBe(tickMs(history[5]));
    expect(stepFrom(history, tickMs(history[8]), 1)).toBeNull(); // onto the latest tick: live
    expect(stepFrom(history, null, 1)).toBeNull();
    expect(stepFrom([], null, -1)).toBeNull();
  });
});

describe('the money at an earlier moment', () => {
  it('is today’s totals minus what every later tick added, per city too', () => {
    const t = totals();
    const at4 = totalsAt(t, history, 4);
    const earnedTo4 = history.slice(0, 5).reduce((s, x) => s + (x.revenue_usd.fleet as number), 0);
    expect(at4.revenue_usd).toBeCloseTo(100 + earnedTo4, 9);
    expect(at4.sold_kwh).toBeCloseTo(50 + (1200 * 5) / 12, 9);
    expect(at4.bought_kwh).toBeCloseTo(20 + (600 * 5) / 12, 9);
    expect(at4.ticks).toBe(25);
    expect(at4.by_location?.LZ_HOUSTON.revenue_usd).toBeCloseTo(60 + history.slice(0, 5).reduce((s, x) => s + (x.revenue_usd.LZ_HOUSTON as number), 0), 9);
    expect(at4.by_location?.LZ_HOUSTON.sold_kwh).toBeCloseTo(50 + 500, 9);
    expect(at4.by_location?.LZ_NORTH.bought_kwh).toBeCloseTo(20 + 250, 9);
    // The latest tick is today's totals, the same object.
    expect(totalsAt(t, history, 9)).toBe(t);
  });
});

describe('the controllers at an earlier moment, rebuilt from the events', () => {
  const template = [worker(0, { LZ_HOUSTON: 5 }), worker(1, { LZ_HOUSTON: 5 }), worker(2, { LZ_NORTH: 5 }), worker(3, { LZ_NORTH: 5 })];
  const events = [
    event('worker_adopted', 6, { worker_id: 1, adopted_by: [0, 2, 3] }),
    event('worker_dead', 6, { worker_id: 1 }),
    event('worker_suspect', 5, { worker_id: 1 }),
  ]; // newest first, as LiveState keeps them
  const cities = { size: 20, cityOf: Uint8Array.from([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]), locations: ['LZ_HOUSTON', 'LZ_NORTH'] };

  it('are all working before the crash', () => {
    expect(workersAt(template, events, tickMs(history[4]), cities).map((w) => w.status)).toEqual(['healthy', 'healthy', 'healthy', 'healthy']);
  });

  it('show "not responding", then crashed and handed over, with the batteries moved round-robin', () => {
    expect(workersAt(template, events, tickMs(history[5]), cities)[1].status).toBe('suspect');
    const after = workersAt(template, events, tickMs(history[6]), cities);
    expect(after.map((w) => w.status)).toEqual(['healthy', 'dead', 'healthy', 'healthy']);
    expect(after.map((w) => w.batteries)).toEqual([7, 0, 7, 6]);
    expect(after[2].by_location).toEqual({ LZ_HOUSTON: 2, LZ_NORTH: 5 });
    expect(after[0].adopted_from).toEqual([1]);
  });

  it('take its own batteries back after a restart (worker_recovered)', () => {
    const restarted = [event('worker_recovered', 8, { worker_id: 1, controller: 2, batteries: 5 }), ...events];
    const back = workersAt(template, restarted, tickMs(history[8]), cities);
    expect(back.map((w) => w.status)).toEqual(['healthy', 'healthy', 'healthy', 'healthy']);
    expect(back.map((w) => w.batteries)).toEqual([5, 5, 5, 5]);
    expect(back[1].by_location).toEqual({ LZ_HOUSTON: 5 });
    expect(back.every((w) => w.adopted_from.length === 0)).toBe(true);
  });

  it('leave their charge unknown: it is not in the events', () => {
    const charged = template.map((w) => ({ ...w, charge_level: 0.6, usable_charge: 0.5 }));
    for (const w of workersAt(charged, events, tickMs(history[4]), cities)) {
      expect(w.usable_charge).toBeUndefined();
      expect(w.charge_level).toBeUndefined();
    }
  });
});

describe('pastState: the live view as it was', () => {
  const live: LiveState = {
    ...initialLiveState,
    received: true,
    runId: 'run-1',
    status: 'running',
    simTimeUtc: history[9].ts_utc,
    history,
    totals: totals(),
    workers: [worker(0, { LZ_HOUSTON: 5 })],
    availability: { available: 18_000, total: 20_000, by_location: {}, offline_workers: [] },
    events: [event('worker_suspect', 8, { worker_id: 0 }), event('price_spike', 2, {})],
    forecast: {
      LZ_HOUSTON: { location: 'LZ_HOUSTON', series: 'rt_price', model: 'x', issued_utc: history[1].ts_utc, step_minutes: 15, points: [] },
      LZ_NORTH: { location: 'LZ_NORTH', series: 'rt_price', model: 'x', issued_utc: history[8].ts_utc, step_minutes: 15, points: [] },
    },
    race: { strategy: 'bands', label: 'Simple rule', revenue_usd: 5, by_location: {}, charge_level: 0.5, live_minus_race_usd: 1 },
    batteryStatus: 'AAAA',
  };

  it('ends the history at that tick, with the money, events and forecasts of then', () => {
    const s = pastState(live, tickMs(history[3]) + 60_000, null);
    expect(s.simTimeUtc).toBe(history[3].ts_utc);
    expect(s.history).toHaveLength(4);
    expect(s.history.at(-1)).toBe(history[3]);
    expect(s.totals.revenue_usd).toBeCloseTo(totalsAt(live.totals, history, 3).revenue_usd, 9);
    expect(s.events.map((e) => e.kind)).toEqual(['price_spike']);
    expect(Object.keys(s.forecast)).toEqual(['LZ_HOUSTON']);
    // The run's own facts stay live.
    expect([s.status, s.runId, s.settings]).toEqual([live.status, live.runId, live.settings]);
  });

  it('takes the controllers (with their charge), availability and race from what this page recorded then', () => {
    const record: FrameRecord = {
      tMs: tickMs(history[3]),
      status: new Uint8Array(10_000),
      workers: [{ ...worker(0, {}), usable_charge: 0.25, charge_level: 0.4 }],
      availability: { available: 20_000, total: 20_000, by_location: {}, offline_workers: [] },
      race: { ...(live.race as NonNullable<LiveState['race']>), revenue_usd: 2 },
    };
    const s = pastState(live, tickMs(history[3]), record);
    expect(s.workers).toBe(record.workers);
    expect(s.workers[0].usable_charge).toBe(0.25);
    expect(s.availability).toBe(record.availability);
    expect(s.race?.revenue_usd).toBe(2);
    expect(s.batteryStatus).toBeNull(); // the map reads the recorded status itself
  });

  it('before the page was opened: no availability or race, the controllers rebuilt from the events', () => {
    const s = pastState(live, tickMs(history[2]), null);
    expect(s.availability).toBeNull();
    expect(s.race).toBeNull();
    expect(s.workers.map((w) => w.status)).toEqual(['healthy']); // the suspect event came later
  });
});

describe('money on predicted ticks is projected', () => {
  it('sums the revenue of predicted ticks, for the fleet or a city', () => {
    const predictedRevenue = history.slice(7).reduce((s, t) => s + (t.revenue_usd.fleet as number), 0);
    expect(projectedMoney(history, null)).toEqual({ usd: predictedRevenue, exact: true });
    expect(projectedMoney(history, 'LZ_HOUSTON')?.usd).toBeCloseTo(history.slice(7).reduce((s, t) => s + (t.revenue_usd.LZ_HOUSTON as number), 0), 9);
    expect(projectedMoney(history.slice(0, 7), null)).toBeNull();
    // A kept history that starts inside the prediction cannot say how much.
    expect(projectedMoney(history.slice(8), null)?.exact).toBe(false);
  });
});
