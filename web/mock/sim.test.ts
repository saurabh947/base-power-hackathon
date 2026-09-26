import { describe, expect, it } from 'vitest';
import type { FrameMessage, IdleMessage } from '../src/api/types.ts';
import { initialLiveState, liveReducer, type LiveState } from '../src/live/reducer.ts';
import { ControllerFlash } from '../src/map/flash.ts';
import { projectLonLat } from '../src/map/albers.ts';
import { pointInRing } from '../src/map/geometry.ts';
import { readLayout } from '../src/map/layout.ts';
import { CHARGING, countCodes, decodeStatus, DOWN, OFFLINE, SELLING, WAITING } from '../src/map/status.ts';
import { TEXAS_OUTLINE } from '../src/map/texasOutline.ts';
import fixture from './data/ercot_rt_lz_preset_days_2025.json' with { type: 'json' };
import { batteryId, batteryIndex, COUNT, LOCS, MockSim, mockSettingsResponse, PRESETS, priceAt } from './sim.ts';

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
  it('holds 2 real days of 15-minute prices for every preset and all four cities', () => {
    expect(fixture.source_note).toContain('REAL DATA');
    expect(fixture.locations).toEqual(['LZ_HOUSTON', 'LZ_NORTH', 'LZ_CPS', 'LZ_AEN']);
    expect(fixture.segments.map((s) => s.preset)).toEqual(PRESETS.map((p) => p.id));
    for (const s of fixture.segments) {
      expect(s.rows).toHaveLength(192);
      for (const r of s.rows) for (const loc of LOCS) expect(typeof (r as Record<string, unknown>)[loc]).toBe('number');
    }
  });

  it('has the documented spikes', () => {
    expect(priceAt('LZ_HOUSTON', at('2025-05-17T00:45:00Z'))).toBe(2250.15); // 7:45 pm CDT, May 16
    expect(priceAt('LZ_HOUSTON', at('2025-05-17T00:55:00Z'))).toBe(2250.15); // forward-filled in its interval
    expect(priceAt('LZ_NORTH', at('2025-05-17T00:45:00Z'))).toBe(2512.18);
    expect(priceAt('LZ_HOUSTON', at('2025-01-01T00:00:00Z'))).toBeNull();
    // The summer night spike reaches all four cities in the 9 pm interval.
    for (const loc of LOCS) expect(priceAt(loc, at('2025-07-12T02:00:00Z'))).toBeGreaterThan(1700);
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
    expect(s.defaults.preset).toBe('summer_spike'); // config.yaml presets.default
    expect(s.defaults.locations).toEqual({ LZ_HOUSTON: 0.35, LZ_NORTH: 0.4, LZ_CPS: 0.13, LZ_AEN: 0.12 });
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
      for (const loc of LOCS) expect(t.price[loc]).toBe(priceAt(loc, Date.parse(t.ts_utc) / 1000));
      expect(Object.keys(t.target_kw)).toEqual(['LZ_HOUSTON', 'LZ_NORTH', 'LZ_CPS', 'LZ_AEN', 'fleet']);
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
    expect(state.totals.revenue_usd).toBeGreaterThan(0); // the summer night spike pays
    expect(state.batteryStatus).toEqual(expect.any(String));
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
    expect(adopted?.message).toMatch(/^Controller 7's 1,000 batteries in Houston were handed to the other 19 controllers in \d+ ms\.$/);
    expect(adopted?.detail).toMatchObject({ worker_id: 6, controller: 7, by_location: { LZ_HOUSTON: 1000 } });
    expect(suspect?.detail).toMatchObject({ by_location: { LZ_HOUSTON: 1000 } });
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

  it('uses fleet-wide battery ids in city blocks: Houston, Dallas–Fort Worth, San Antonio, Austin', () => {
    const { sim } = running();
    expect(COUNT).toEqual({ LZ_HOUSTON: 7000, LZ_NORTH: 8000, LZ_CPS: 2600, LZ_AEN: 2400 });
    expect(batteryId(123)).toBe('b00123');
    expect(batteryIndex('b10000')).toBe(10_000);
    expect(batteryIndex('LZ_HOUSTON-000123')).toBeNull();
    expect(sim.battery('b06999')).toMatchObject({ location: 'LZ_HOUSTON', city: 'Houston' });
    expect(sim.battery('b07000')).toMatchObject({ location: 'LZ_NORTH', city: 'Dallas–Fort Worth' });
    expect(sim.battery('b14999').location).toBe('LZ_NORTH');
    expect(sim.battery('b15000')).toMatchObject({ location: 'LZ_CPS', city: 'San Antonio' });
    expect(sim.battery('b17600')).toMatchObject({ location: 'LZ_AEN', city: 'Austin' });
    expect(sim.battery('b17600').lonlat).toHaveLength(2);
    expect(caught(() => sim.battery('b20000'))).toMatchObject({ status: 404 });
    expect(sim.setAvailability({ scope: 'battery', battery_id: 'b00042', available: false }).available).toBe(19_999);
  });

  it('takes the same batteries offline for the same share of an area', () => {
    const { sim } = running();
    const r1 = sim.setAvailability({ scope: 'location', location: 'LZ_HOUSTON', offline_fraction: 0.3 });
    expect(r1.affected).toBe(2100);
    expect(r1.available).toBe(17_900);
    expect(sim.setAvailability({ scope: 'location', location: 'LZ_HOUSTON', offline_fraction: 0.3 }).available).toBe(17_900);
    expect(sim.availability().by_location.LZ_HOUSTON).toEqual({ available: 4900, total: 7000 });
    expect(sim.availability().by_location.LZ_AEN).toEqual({ available: 2400, total: 2400 });
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
    expect(Object.keys(sim.results().strategies[0].per_location_usd)).toEqual([...LOCS]);
    expect(caught(() => sim.backtestStatus('nope'))).toMatchObject({ status: 404 });
  });

  it('gives each controller a contiguous block of 1,000, and moves them on a crash (workers[].by_location)', () => {
    const { sim, step } = running(1);
    const before = frame(step()).workers;
    for (const w of before) expect(w.batteries).toBe(1000);
    expect(before[0].by_location).toEqual({ LZ_HOUSTON: 1000 });
    expect(before[6].by_location).toEqual({ LZ_HOUSTON: 1000 });
    expect(before[7].by_location).toEqual({ LZ_NORTH: 1000 });
    expect(before[17].by_location).toEqual({ LZ_CPS: 600, LZ_AEN: 400 });
    expect(before[19].by_location).toEqual({ LZ_AEN: 1000 });
    sim.kill(3);
    step();
    const after = frame(step()).workers;
    expect(after.find((w) => w.worker_id === 3)?.by_location).toEqual({});
    for (const loc of LOCS) expect(after.reduce((s, w) => s + (w.by_location?.[loc] ?? 0), 0)).toBe(COUNT[loc]);
  });

  it('sends totals by city that add up to the fleet totals', () => {
    const { step } = running(10);
    let f = frame(step());
    for (let i = 0; i < 12; i++) f = frame(step());
    const by = f.totals.by_location ?? {};
    expect(Object.keys(by)).toEqual([...LOCS]);
    const sum = (k: 'revenue_usd' | 'sold_kwh' | 'bought_kwh') => LOCS.reduce((s, l) => s + (by[l]?.[k] ?? 0), 0);
    expect(sum('revenue_usd')).toBeCloseTo(f.totals.revenue_usd, 1);
    expect(sum('sold_kwh')).toBeCloseTo(f.totals.sold_kwh, 0);
    expect(sum('bought_kwh')).toBeCloseTo(f.totals.bought_kwh, 0);
  });
});

describe('GET /fleet/layout (simulated positions)', () => {
  const sim = new MockSim();
  const raw = sim.layout();
  const layout = readLayout(raw);

  it('matches docs/API.md: 4 cities in id blocks, one position pair per battery', () => {
    expect(raw.fleet_size).toBe(20_000);
    expect(raw.cities.map((c) => [c.location, c.batteries, c.first_battery])).toEqual([
      ['LZ_HOUSTON', 7000, 0],
      ['LZ_NORTH', 8000, 7000],
      ['LZ_CPS', 2600, 15000],
      ['LZ_AEN', 2400, 17600],
    ]);
    expect(raw.cities[1]).toMatchObject({ name: 'Dallas–Fort Worth', short: 'DFW' });
    expect(Buffer.from(raw.positions, 'base64')).toHaveLength(80_000);
    expect(layout?.size).toBe(20_000);
  });

  it('puts every battery inside Texas, near its own city', () => {
    if (!layout) throw new Error('no layout');
    for (let i = 0; i < layout.size; i += 7) {
      expect(pointInRing(layout.x[i], layout.y[i], TEXAS_OUTLINE)).toBe(true);
      const city = layout.cities[layout.cityOf[i]];
      const [cx, cy] = projectLonLat(city.center[0], city.center[1]);
      expect(Math.hypot(layout.x[i] - cx, layout.y[i] - cy)).toBeLessThan(90_000); // metres
    }
  });

  it('is the same every time (seeded)', () => {
    expect(new MockSim().layout().positions).toBe(raw.positions);
  });
});

const codesOf = (m: { battery_status?: string }) => decodeStatus(m.battery_status ?? '', 20_000) ?? new Uint8Array(0);

describe('battery_status', () => {
  it('is in snapshots and frames: waiting before the run, then each city follows its action', () => {
    const idle = new MockSim();
    expect(countCodes(codesOf(idle.snapshot()), null, null)).toEqual([0, 20_000, 0, 0, 0]);
    const { sim, step } = running(10);
    const seen = new Set<number>();
    for (let i = 0; i < 40; i++) {
      const f = frame(step());
      const codes = codesOf(f);
      const last = f.ticks[f.ticks.length - 1];
      if (!last) continue;
      for (const loc of LOCS) {
        const first = sim.layout().cities.find((c) => c.location === loc)?.first_battery ?? 0;
        const expected = { sell: SELLING, charge: CHARGING, hold: WAITING }[last.action[loc] ?? 'hold'];
        // A city's batteries follow its action, except those already full or down to their reserve (waiting).
        const cityCodes = new Set(codes.subarray(first, first + COUNT[loc]));
        for (const c of cityCodes) expect([expected, WAITING]).toContain(c);
        for (const c of cityCodes) seen.add(c);
      }
    }
    expect(seen.has(SELLING)).toBe(true);
  });

  it('shows offline batteries as 0', () => {
    const { sim, step } = running(1);
    sim.setAvailability({ scope: 'location', location: 'LZ_AEN', offline_fraction: 0.5 });
    const counts = countCodes(codesOf(frame(step())), null, null);
    expect(counts[OFFLINE]).toBe(1200);
  });

  it("shows a crashed controller's batteries as 4 for its two missed steps, then back once handed over", () => {
    const { sim, step } = running(2);
    step();
    sim.kill(5);
    const f1 = frame(step()); // both missed steps fall in this frame; the hand-over comes after the second
    expect(f1.events.map((e) => e.kind)).toEqual(expect.arrayContaining(['worker_suspect', 'worker_dead', 'worker_adopted']));
    const down = codesOf(f1);
    expect(countCodes(down, null, null)[DOWN]).toBe(1000);
    // Worker 5 runs b05000-b05999, all in Houston.
    expect(down[5000]).toBe(DOWN);
    expect(down[5999]).toBe(DOWN);
    expect(down[6000]).not.toBe(DOWN);
    const f2 = frame(step());
    expect(countCodes(codesOf(f2), null, null)[DOWN]).toBe(0);
  });

  it('can miss code 4 at 5 steps a second, like the backend; the map then flashes from the events', () => {
    const { sim, step } = running(5);
    let state: LiveState = liveReducer(initialLiveState, sim.snapshot());
    state = liveReducer(state, step());
    const flash = new ControllerFlash();
    flash.reset(20_000, 20, state.events);
    sim.kill(8); // Dallas–Fort Worth, b08000-b08999
    const f1 = frame(step());
    state = liveReducer(state, f1);
    const codes = codesOf(f1);
    expect(countCodes(codes, null, null)[DOWN]).toBe(0); // the last of the 5 steps is after the hand-over
    const wall = Date.parse(f1.events[f1.events.length - 1].wall_utc); // the simulated wall clock
    expect(flash.apply(state.events, codes, wall)).toBe(1000);
    expect(codes[8000]).toBe(DOWN);
    expect(codes[8999]).toBe(DOWN);
    // The next frame: handed over, nothing new, so they come back.
    const f2 = frame(step());
    state = liveReducer(state, f2);
    const next = codesOf(f2);
    expect(flash.apply(state.events, next, wall + 1000)).toBe(0);
    expect(countCodes(next, null, null)[DOWN]).toBe(0);
  });
});
