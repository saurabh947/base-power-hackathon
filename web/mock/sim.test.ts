import { describe, expect, it } from 'vitest';
import type { FrameMessage } from '../src/api/types.ts';
import { initialLiveState, liveReducer, type LiveState } from '../src/live/reducer.ts';
import fixture from './data/ercot_rt_lz_2025-05-16_17.json' with { type: 'json' };
import { MockError, MockSim, priceAt } from './sim.ts';

function running(speed = 2) {
  let now = Date.parse('2026-09-25T12:00:00Z');
  const sim = new MockSim({ now: () => now, random: () => 0.5, backtestMs: 1000 });
  sim.start({ speed_tps: speed });
  const step = () => {
    now += 1000;
    return sim.stepWall();
  };
  step();
  step(); // past the 1.5 s "starting" phase
  return { sim, step, advance: (ms: number) => (now += ms) };
}

describe('price fixture', () => {
  it('holds 192 real 15-minute rows per area with the documented spike', () => {
    expect(fixture.rows).toHaveLength(192);
    expect(fixture.source_note).toContain('REAL DATA');
    const spikeS = Date.parse('2025-05-17T00:45:00Z') / 1000; // 7:45 pm CDT
    expect(priceAt('LZ_HOUSTON', spikeS)).toBe(2250.15);
    expect(priceAt('LZ_HOUSTON', spikeS + 600)).toBe(2250.15); // forward-filled within the interval
    expect(priceAt('LZ_NORTH', spikeS)).toBe(2512.18);
  });
});

describe('MockSim', () => {
  it('starts, then emits speed_tps ticks per second with real prices', () => {
    const { sim, step } = running(5);
    expect(sim.status).toBe('running');
    const frame = step() as FrameMessage;
    expect(frame.type).toBe('frame');
    expect(frame.ticks).toHaveLength(5);
    for (const t of frame.ticks) {
      expect(t.price.LZ_HOUSTON).toBe(priceAt('LZ_HOUSTON', Date.parse(t.ts_utc) / 1000));
      expect(Object.keys(t.target_kw)).toEqual(['LZ_HOUSTON', 'LZ_NORTH', 'fleet']);
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

  it('crashes a controller: not responding, then crashed and adopted, and never double-counts batteries', () => {
    const { sim, step } = running(1);
    const killed = sim.kill(7);
    expect(killed.worker_id).toBe(7);
    expect(() => sim.kill(7)).toThrow(MockError);

    const f1 = step() as FrameMessage;
    expect(f1.workers.find((w) => w.worker_id === 7)?.status).toBe('suspect');
    expect(f1.events.some((e) => e.kind === 'worker_suspect')).toBe(true);

    const f2 = step() as FrameMessage;
    const dead = f2.workers.find((w) => w.worker_id === 7);
    expect(dead?.status).toBe('dead');
    expect(dead?.batteries).toBe(0);
    expect(f2.workers.reduce((a, w) => a + w.batteries, 0)).toBe(20_000);
    expect(f2.workers.filter((w) => w.adopted_from.includes(7))).toHaveLength(19);
    expect(f2.events.map((e) => e.kind)).toEqual(expect.arrayContaining(['worker_dead', 'worker_adopted']));
  });

  it('refuses to leave fewer than 2 healthy controllers', () => {
    const { sim, step } = running(1);
    for (let id = 1; id <= 18; id++) sim.kill(id);
    step();
    step();
    expect(() => sim.kill(19)).toThrow(/At least 2 controllers/);
  });

  it('takes the same batteries offline for the same share of an area', () => {
    const { sim } = running();
    const r1 = sim.setAvailability({ scope: 'location', location: 'LZ_HOUSTON', offline_fraction: 0.3 });
    expect(r1.affected).toBe(3000);
    expect(r1.available).toBe(17_000);
    const r2 = sim.setAvailability({ scope: 'location', location: 'LZ_HOUSTON', offline_fraction: 0.3 });
    expect(r2.available).toBe(17_000);
    sim.setAvailability({ scope: 'worker', worker_id: 12, available: false });
    expect(sim.availability().offline_workers).toEqual([12]);
    expect(sim.setAvailability({ scope: 'all', available: true }).available).toBe(20_000);
  });

  it('only has prices for the spike evening, and says so with a 501', () => {
    const sim = new MockSim();
    const attempt = () => sim.start({ preset: 'morning_spike' });
    const caught = (() => {
      try {
        attempt();
        return null;
      } catch (err) {
        return err;
      }
    })();
    expect(caught).toBeInstanceOf(MockError);
    expect(caught).toMatchObject({ status: 501, code: 'not_in_mock' });
  });

  it('labels its full-year numbers as simulated', () => {
    const sim = new MockSim();
    const results = sim.results();
    expect(results.assumptions[0]).toMatch(/^SIMULATED/);
    expect(results.strategies.filter((s) => s.is_headline)).toHaveLength(1);
  });
});
