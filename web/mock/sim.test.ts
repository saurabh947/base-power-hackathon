import { describe, expect, it } from 'vitest';
import type { FrameMessage, IdleMessage, LiveEvent } from '../src/api/types.ts';
import { initialLiveState, liveReducer, type LiveState } from '../src/live/reducer.ts';
import { gainsView, readAiGains } from '../src/screens/results/aiGains.ts';
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
  it('holds real 15-minute prices for every 2025 preset (2 days) and the mock-only days, all four cities', () => {
    expect(fixture.source_note).toContain('REAL DATA');
    expect(fixture.locations).toEqual(['LZ_HOUSTON', 'LZ_NORTH', 'LZ_CPS', 'LZ_AEN']);
    const presets2025 = PRESETS.filter((p) => p.year === 2025).map((p) => p.id);
    expect(fixture.segments.map((s) => s.preset)).toEqual([...presets2025.filter((id) => id !== 'jan1_2025'), 'jan1_2025', 'stand_in_2026_sep']);
    for (const s of fixture.segments) {
      expect(s.rows).toHaveLength(s.preset === 'stand_in_2026_sep' ? 7 * 96 : 192);
      for (const r of s.rows) for (const loc of LOCS) expect(typeof (r as Record<string, unknown>)[loc]).toBe('number');
    }
  });

  it('has the documented spikes', () => {
    expect(priceAt('LZ_HOUSTON', at('2025-05-17T00:45:00Z'))).toBe(2250.15); // 7:45 pm CDT, May 16
    expect(priceAt('LZ_HOUSTON', at('2025-05-17T00:55:00Z'))).toBe(2250.15); // forward-filled in its interval
    expect(priceAt('LZ_NORTH', at('2025-05-17T00:45:00Z'))).toBe(2512.18);
    expect(priceAt('LZ_HOUSTON', at('2025-03-01T00:00:00Z'))).toBeNull();
    // The summer night spike reaches all four cities in the 9 pm interval.
    for (const loc of LOCS) expect(priceAt(loc, at('2025-07-12T02:00:00Z'))).toBeGreaterThan(1700);
  });
});

describe('GET /settings shapes', () => {
  it('match docs/API.md', () => {
    const s = mockSettingsResponse();
    expect(Object.keys(s.presets[0]).toSorted()).toEqual(
      ['date_local', 'description', 'id', 'label', 'start_local', 'start_utc', 'year'].toSorted(),
    );
    expect(Object.keys(s.strategies[0]).toSorted()).toEqual(['description', 'id', 'label']);
    expect(s.speeds[1]).toEqual({ tps: 2, label: '1 hour every 6 s' });
    expect(s.presets.map((p) => p.id)).toEqual([
      'spike_evening',
      'morning_spike',
      'summer_spike',
      'ai_best_evening',
      'ordinary_day',
      'jan1_2025',
      'predicted_2026',
      'jan1_2026',
    ]);
    expect(s.defaults).toMatchObject({ year: 2025, preset: 'ai_best_evening' }); // config.yaml presets.default
    expect(s.defaults.locations).toEqual({ LZ_HOUSTON: 0.35, LZ_NORTH: 0.4, LZ_CPS: 0.13, LZ_AEN: 0.12 });
    expect(s.editable).toEqual(['year', 'preset', 'speed_tps', 'strategy', 'start_charge_level']);
  });

  it('offer 2025 and 2026, each with its own presets and a "From January 1"', () => {
    const s = mockSettingsResponse();
    expect(s.years).toEqual([
      { year: 2025, label: '2025', description: 'Real ERCOT prices for the whole year.', real_until_utc: null },
      { year: 2026, label: '2026', description: 'Real prices through Sep 20, then the AI’s prediction.', real_until_utc: '2026-09-21T05:00:00Z' },
    ]);
    for (const y of [2025, 2026]) {
      const own = s.presets.filter((p) => p.year === y);
      expect(own.length).toBeGreaterThan(0);
      expect(own.some((p) => p.label === 'From January 1' && p.date_local === `${y}-01-01`)).toBe(true);
      for (const p of own) expect(p.date_local.startsWith(String(y))).toBe(true);
    }
    expect(s.presets.some((p) => p.year === 2024)).toBe(false);
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

  it('stops a running full-year job (POST /backtest/{job_id}/cancel), keeping the previous results', () => {
    let now = 0;
    const sim = new MockSim({ now: () => now, resultsBeforeBacktest: false, backtestMs: 1000 });
    const job = sim.startBacktest();
    expect(sim.cancelBacktest(job.job_id)).toEqual({ status: 'cancelled' });
    expect(sim.backtestStatus(job.job_id)).toMatchObject({ status: 'cancelled', run_ids: [] });
    // Stopped already: 409 not_running; an unknown id: 404.
    expect(caught(() => sim.cancelBacktest(job.job_id))).toMatchObject({ status: 409, code: 'not_running' });
    expect(caught(() => sim.cancelBacktest('nope'))).toMatchObject({ status: 404 });
    // Nothing was stored, even after the time it would have taken.
    now += 5000;
    expect(sim.backtestStatus(job.job_id).status).toBe('cancelled');
    expect(caught(() => sim.results())).toMatchObject({ status: 404, code: 'no_results' });
    // A new re-run is a new job, and one that has finished cannot be stopped.
    const next = sim.startBacktest();
    expect(next.job_id).not.toBe(job.job_id);
    now += 1000;
    expect(sim.backtestStatus(next.job_id).status).toBe('done');
    expect(caught(() => sim.cancelBacktest(next.job_id))).toMatchObject({ status: 409, code: 'not_running' });
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

/** The summer evening with the simple rule live (so the race runs the AI forecast), past its starting phase. */
function simpleRuleRun() {
  let now = 0;
  const sim = new MockSim({ now: () => now, random: () => 0.5 });
  sim.start({ strategy: 'bands', speed_tps: 10 });
  now += 2000;
  sim.stepWall();
  return { sim, step: () => ((now += 1000), sim.stepWall()) };
}

describe('the race (docs/API.md Race)', () => {
  it('runs the simple rule beside the AI forecast, in every frame and snapshot while a run exists', () => {
    const { sim, step } = running(10);
    let f = frame(step());
    for (let i = 0; i < 5; i++) f = frame(step());
    expect(f.race).toMatchObject({ strategy: 'bands', label: 'Simple rule' });
    expect(Object.keys(f.race?.by_location ?? {})).toEqual([...LOCS]);
    for (const loc of LOCS) {
      expect(f.race?.by_location[loc]).toEqual({ revenue_usd: expect.any(Number), charge_level: expect.any(Number), action: expect.stringMatching(/^(charge|sell|hold)$/) });
    }
    const byCity = LOCS.reduce((s, l) => s + (f.race?.by_location[l]?.revenue_usd ?? 0), 0);
    expect(byCity).toBeCloseTo(f.race?.revenue_usd ?? Number.NaN, 2);
    expect(f.race?.live_minus_race_usd).toBeCloseTo(f.totals.revenue_usd - (f.race?.revenue_usd ?? 0), 3);
    expect(sim.snapshot().race?.strategy).toBe('bands');
    sim.reset();
    expect(sim.snapshot().race).toBeNull();
  });

  it('races the AI forecast when the live run itself uses the simple rule', () => {
    expect(frame(simpleRuleRun().step()).race).toMatchObject({ strategy: 'forecast', label: 'AI forecast (TimesFM-3)' });
  });

  it('notes the summer night spike: the charge each side had, then who is ahead', () => {
    const { sim, step } = running(10, 'summer_spike');
    const updates: LiveEvent[] = [];
    for (let i = 0; i < 60 && sim.status !== 'finished'; i++) updates.push(...frame(step()).events.filter((e) => e.kind === 'race_update'));
    expect(updates[0].message).toMatch(/^Prices jumped to \$1,7\d\d in all four cities\. The AI fleet had \d+% charge left; the simple rule had \d+%\.$/);
    expect(updates[0].detail).toMatchObject({ race_strategy: 'bands', locations: [...LOCS] });
    expect(updates[1]).toMatchObject({ severity: 'good', message: expect.stringMatching(/^The AI is now \$[\d,]+ ahead of the simple rule\.$/) });
    expect(sim.snapshot().race?.live_minus_race_usd).toBeGreaterThan(0);
  });

  it('mirrors availability, and the charge a crash empties, but not the crash itself', () => {
    const offline = running(10, 'spike_evening');
    offline.sim.setAvailability({ scope: 'location', location: 'LZ_HOUSTON', offline_fraction: 1 });
    const before = offline.sim.snapshot().race?.by_location.LZ_HOUSTON;
    for (let i = 0; i < 20; i++) offline.step();
    // With every Houston battery offline, the race's Houston batteries sit still too.
    expect(offline.sim.snapshot().race?.by_location.LZ_HOUSTON).toEqual({ ...before, action: 'hold' });
    expect(offline.sim.snapshot().race?.by_location.LZ_NORTH?.revenue_usd).not.toBe(0);

    // docs/API.md Mirroring: a crash empties that controller's batteries in the race too, so the race compares
    // only decisions; the all-online potential fleet keeps its charge, so the shortfall counts the lost energy.
    const calm = simpleRuleRun();
    const crash = simpleRuleRun();
    crash.sim.kill(0);
    const houston = (s: ReturnType<typeof calm.sim.snapshot>) => s.race?.by_location.LZ_HOUSTON.charge_level ?? Number.NaN;
    // 1,000 of Houston's 7,000 batteries emptied at once, in the race as well.
    expect(houston(crash.sim.snapshot())).toBeLessThan(houston(calm.sim.snapshot()) - 0.05);
    for (let i = 0; i < 6; i++) {
      calm.step();
      crash.step();
    }
    const [a, b] = [calm.sim.snapshot(), crash.sim.snapshot()];
    expect(b.totals.revenue_usd).not.toBe(a.totals.revenue_usd);
    expect(b.race?.potential?.revenue_usd).toBe(a.race?.potential?.revenue_usd);
    // The crash shows the third bar; the calm run does not need it.
    expect([a.race?.shows_potential, b.race?.shows_potential]).toEqual([false, true]);
  });
});

describe('GET /results/ai-gains (the measured numbers, RUNLOG.md)', () => {
  const gains = new MockSim().aiGains();

  it('has the pitch year only (2025), with the uplift over the simple rule', () => {
    expect(gains).toMatchObject({ ai: 'forecast', baseline: 'bands' });
    expect(gains.years.map((y) => [y.year, y.is_pitch])).toEqual([[2025, true]]);
    expect(gains.years[0].uplift).toEqual({ per_battery_usd_year: 67.28, pct: 0.544, fleet_usd_year: 1_345_600 });
    expect(gains.years[0].best_possible_per_battery_usd_year).toBe(421.29);
  });

  it('matches the docs/API.md example and the RUNLOG table', () => {
    expect(gains.years[0].strategies.forecast).toMatchObject({
      label: 'AI forecast (TimesFM-3)',
      per_battery_usd_year: 190.95,
      fleet_usd_year: 3_819_000,
      share_of_best: 0.453,
      surplus_mwh: 7011.3,
      spike_mwh: 25.2,
      spike_events: 21,
      spike_charge_level: 0.231,
      sold_mwh: 80_168.1,
      avg_sell_usd_mwh: 66.58,
      avg_buy_usd_mwh: 17.02,
    });
    // bought_mwh is derived so the money adds up: sold × average sell price − bought × average buy price.
    for (const y of gains.years) {
      for (const s of Object.values(y.strategies)) {
        expect(s.sold_mwh * s.avg_sell_usd_mwh - s.bought_mwh * s.avg_buy_usd_mwh).toBeCloseTo(s.fleet_usd_year, -1);
      }
    }
  });

  it('reads on the site as the approved headline and cards', () => {
    const read = readAiGains(JSON.parse(JSON.stringify(gains)));
    if (!read) throw new Error('unreadable');
    const view = gainsView(read, 20_000);
    expect(view.headline?.sentence).toBe(
      '+$67 per battery per year (+54%) vs the simple rule, $1.35 million more a year for the 20,000-battery fleet',
    );
    expect(view.perfect?.bars.map((b) => b.text)).toEqual(['29%', '32%', '45%']);
    expect(view.surplus).toMatchObject({ aiText: '7,011 MWh', baseText: '1,239 MWh', times: '5.7×' });
    expect(Object.keys(view)).toEqual(['year', 'headline', 'earns', 'perfect', 'surplus']); // three cards, no spike card
  });

  it('agrees with GET /results/pitch', () => {
    const results = new MockSim().results();
    const per = Object.fromEntries(results.strategies.map((s) => [s.strategy, s.per_battery_usd_year]));
    for (const [id, s] of Object.entries(gains.years[0].strategies)) expect(per[id]).toBe(s.per_battery_usd_year);
    expect(results.ceiling?.per_battery_usd_year).toBe(421.29);
    expect(results.year).toBe(2025);
  });

  it('answers 404 no_results before any full-year run, or when told to', () => {
    expect(caught(() => new MockSim({ resultsBeforeBacktest: false }).aiGains())).toMatchObject({ status: 404, code: 'no_results' });
    expect(caught(() => new MockSim({ noAiGains: true }).aiGains())).toMatchObject({ status: 404, code: 'no_results' });
  });
});
