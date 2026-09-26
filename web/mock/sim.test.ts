import { describe, expect, it } from 'vitest';
import type { FrameMessage, IdleMessage } from '../src/api/types.ts';
import { initialLiveState, liveReducer, type LiveState } from '../src/live/reducer.ts';
import fixture from './data/ercot_rt_lz_preset_days_2025.json' with { type: 'json' };
import { batteryId, batteryIndex, MockSim, mockSettingsResponse, PRESETS, priceAt } from './sim.ts';

function running(speed = 2, preset?: string) {
  let now = Date.parse('2026-09-25T12:00:00Z');
  const sim = new MockSim({ now: () => now, random: () => 0.5, backtestMs: 1000 });
  sim.start({ speed_tps: speed, ...(preset ? { preset } : {}) });
  const step = () => {
    now += 1000;
    return sim.stepWall();
  };
  step();
  step(); // past the 1.5 s "starting" phase
  return { sim, step, advance: (ms: number) => (now += ms) };
}

const frame = (m: unknown) => m as FrameMessage;
const at = (iso: string) => Date.parse(iso) / 1000;

function caught(fn: () => unknown): unknown {
  try {
    fn();
    return null;
  } catch (err) {
    return err;
  }
}

describe('price fixture', () => {
  it('holds 2 real days of 15-minute prices for every preset', () => {
    expect(fixture.source_note).toContain('REAL DATA');
    expect(fixture.segments.map((s) => s.preset)).toEqual(PRESETS.map((p) => p.id));
    for (const s of fixture.segments) expect(s.rows).toHaveLength(192);
  });

  it('has the documented spikes', () => {
    expect(priceAt('LZ_HOUSTON', at('2025-05-17T00:45:00Z'))).toBe(2250.15); // 7:45 pm CDT, May 16
    expect(priceAt('LZ_HOUSTON', at('2025-05-17T00:55:00Z'))).toBe(2250.15); // forward-filled in its interval
    expect(priceAt('LZ_NORTH', at('2025-05-17T00:45:00Z'))).toBe(2512.18);
    expect(priceAt('LZ_HOUSTON', at('2025-01-01T00:00:00Z'))).toBeNull();
  });
});

describe('GET /settings shapes', () => {
  it('match docs/API.md', () => {
    const s = mockSettingsResponse();
    expect(Object.keys(s.presets[0]).toSorted()).toEqual(
      ['date_local', 'description', 'id', 'label', 'start_local', 'start_utc'].toSorted(),
    );
    expect(Object.keys(s.strategies[0]).toSorted()).toEqual(['description', 'id', 'label']);
    expect(s.speeds[1]).toEqual({ tps: 2, label: '1 hour every 6 s' });
    expect(s.presets.map((p) => p.id)).toEqual(['spike_evening', 'morning_spike', 'summer_spike', 'ordinary_day']);
  });
});

describe('MockSim', () => {
  it('starts, then emits speed_tps ticks per second with real prices', () => {
    const { sim, step } = running(5);
    expect(sim.status).toBe('running');
    const f = frame(step());
    expect(f.type).toBe('frame');
    expect(f.ticks).toHaveLength(5);
    for (const t of f.ticks) {
      expect(t.price.LZ_HOUSTON).toBe(priceAt('LZ_HOUSTON', Date.parse(t.ts_utc) / 1000));
      expect(Object.keys(t.target_kw)).toEqual(['LZ_HOUSTON', 'LZ_NORTH', 'fleet']);
    }
  });

  it('replays every preset from real prices', () => {
    for (const p of PRESETS) {
      const { sim, step } = running(10, p.id);
      step();
      const history = sim.snapshot().history;
      expect(history[0].ts_utc).toBe(p.start_utc);
      expect(history).toHaveLength(20);
      for (const t of history) expect(t.price.LZ_NORTH).toBe(priceAt('LZ_NORTH', Date.parse(t.ts_utc) / 1000));
    }
  });

  it('produces frames the reducer applies without gaps', () => {
    const { sim, step } = running(10);
    let state: LiveState = liveReducer(initialLiveState, sim.snapshot());
    for (let i = 0; i < 30; i++) state = liveReducer(state, step());
    expect(state.history.length).toBe(state.totals.ticks);
    const times = state.history.map((t) => Date.parse(t.ts_utc));
    expect(times.every((t, i) => i === 0 || t - times[i - 1] === 300_000)).toBe(true);
    expect(state.totals.revenue_usd).toBeGreaterThan(0); // the spike evening pays
  });

  it('finishes at the end of the loaded prices with a run_finished event', () => {
    const { sim, step } = running(10);
    const kinds: string[] = [];
    for (let i = 0; i < 60 && sim.status !== 'finished'; i++) kinds.push(...frame(step()).events.map((e) => e.kind));
    expect(sim.status).toBe('finished');
    expect(kinds).toContain('run_finished');
    expect(frame(step()).status).toBe('finished');
  });

  it('numbers workers 0-19 and names them Controller 1-20', () => {
    const { sim, step } = running(1);
    const workers = frame(step()).workers;
    expect(workers.map((w) => w.worker_id)).toEqual(Array.from({ length: 20 }, (_, i) => i));
    expect(sim.battery('b00000').worker_id).toBe(0);
    expect(sim.battery('b19999').worker_id).toBe(19);
  });

  it('crashes a controller: not responding, then crashed and adopted, with the pinned event details', () => {
    const { sim, step } = running(1);
    expect(sim.kill(6).worker_id).toBe(6);
    expect(caught(() => sim.kill(6))).toMatchObject({ status: 409, code: 'already_dead' });

    const f1 = frame(step());
    expect(f1.workers.find((w) => w.worker_id === 6)?.status).toBe('suspect');
    const suspect = f1.events.find((e) => e.kind === 'worker_suspect');
    expect(suspect?.message).toMatch(/^Controller 7 stopped responding/);
    expect(suspect?.detail).toMatchObject({ worker_id: 6, controller: 7 });

    const f2 = frame(step());
    const dead = f2.workers.find((w) => w.worker_id === 6);
    expect(dead?.status).toBe('dead');
    expect(dead?.batteries).toBe(0);
    expect(f2.workers.reduce((a, w) => a + w.batteries, 0)).toBe(20_000);
    expect(f2.workers.filter((w) => w.adopted_from.includes(6))).toHaveLength(19);
    const adopted = f2.events.find((e) => e.kind === 'worker_adopted');
    expect(adopted?.message).toMatch(/^Controller 7's 1,000 batteries were handed to the other 19 controllers in \d+ ms\.$/);
    expect(adopted?.detail).toMatchObject({ worker_id: 6, controller: 7 });
    expect(adopted?.detail?.adopted_by).toHaveLength(19);
    expect(typeof adopted?.detail?.ms).toBe('number');
    expect(f2.events.find((e) => e.kind === 'worker_dead')?.detail).toMatchObject({ worker_id: 6, controller: 7 });
  });

  it('refuses to leave fewer than 2 healthy controllers', () => {
    const { sim, step } = running(1);
    for (let id = 0; id < 18; id++) sim.kill(id);
    step();
    step();
    expect(() => sim.kill(18)).toThrow(/At least 2 controllers/);
  });

  it('uses fleet-wide battery ids, Houston first', () => {
    const { sim } = running();
    expect(batteryId(123)).toBe('b00123');
    expect(batteryIndex('b10000')).toBe(10_000);
    expect(batteryIndex('LZ_HOUSTON-000123')).toBeNull();
    expect(sim.battery('b09999').location).toBe('LZ_HOUSTON');
    expect(sim.battery('b10000').location).toBe('LZ_NORTH');
    expect(caught(() => sim.battery('b20000'))).toMatchObject({ status: 404 });
    expect(sim.setAvailability({ scope: 'battery', battery_id: 'b00042', available: false }).available).toBe(19_999);
  });

  it('takes the same batteries offline for the same share of an area', () => {
    const { sim } = running();
    const r1 = sim.setAvailability({ scope: 'location', location: 'LZ_HOUSTON', offline_fraction: 0.3 });
    expect(r1.affected).toBe(3000);
    expect(r1.available).toBe(17_000);
    expect(sim.setAvailability({ scope: 'location', location: 'LZ_HOUSTON', offline_fraction: 0.3 }).available).toBe(17_000);
    sim.setAvailability({ scope: 'worker', worker_id: 12, available: false });
    expect(sim.availability().offline_workers).toEqual([12]);
    expect(sim.setAvailability({ scope: 'all', available: true }).available).toBe(20_000);
  });

  it('sends the pinned idle message every 5 s, remembering the last run', () => {
    const { sim, step } = running(2);
    step();
    const runId = sim.runId;
    sim.reset();
    const first = step() as IdleMessage;
    expect(first).toEqual({
      type: 'idle',
      status: 'idle',
      run_id: null,
      last_run: { run_id: runId, totals: expect.objectContaining({ ticks: 4 }) },
    });
    const next = [step(), step(), step(), step(), step()];
    expect(next.filter(Boolean)).toHaveLength(1);
    expect(sim.snapshot()).toMatchObject({ type: 'snapshot', status: 'idle', run_id: null, sim_time_utc: null, speed_tps: 2 });
  });

  it('answers a second POST /backtest with the running job, then no_results only before any run', () => {
    let now = 0;
    const sim = new MockSim({ now: () => now, resultsBeforeBacktest: false, backtestMs: 1000 });
    expect(caught(() => sim.results())).toMatchObject({ status: 404, code: 'no_results', message: 'No full-year results yet.' });
    const job = sim.startBacktest();
    expect(sim.startBacktest()).toEqual(job);
    expect(sim.backtestStatus(job.job_id).status).toBe('running');
    now += 1000;
    expect(sim.backtestStatus(job.job_id)).toMatchObject({ status: 'done', skipped: {} });
    expect(sim.results().assumptions[0]).toMatch(/^SIMULATED/);
    expect(sim.results().strategies.filter((s) => s.is_headline)).toHaveLength(1);
    expect(caught(() => sim.backtestStatus('nope'))).toMatchObject({ status: 404 });
  });
});
