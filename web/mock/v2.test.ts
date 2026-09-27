// The mock's v2 contract (docs/API.md, commit 4879fde): 2025/2026 with the AI's prediction after 2026's real
// prices end, race.potential, battery_status within one frame while paused, and the public, rate-limited
// POST /chaos/kill.

import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { describe, expect, it } from 'vitest';
import type { FrameMessage, Tick } from '../src/api/types.ts';
import { countCodes, decodeStatus, DOWN, OFFLINE } from '../src/map/status.ts';
import { createMockApi } from './api.ts';
import { isPredicted, LOCS, MockSim, PRESETS, priceAt, REAL_UNTIL_2026 } from './sim.ts';

function running(speed = 2, preset?: string) {
  let now = Date.parse('2026-09-25T12:00:00Z');
  const sim = new MockSim({ now: () => now, random: () => 0.5, backtestMs: 1000 });
  sim.start({ speed_tps: speed, preset: preset ?? 'ai_best_evening' }); // May 30, 2025, from 6 pm
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
const codesOf = (m: { battery_status?: string }) => decodeStatus(m.battery_status ?? '', 20_000) ?? new Uint8Array(0);

/** How much a price series moves from one 15 minutes to the next, on average. */
const roughness = (xs: number[]) => xs.slice(1).reduce((s, x, i) => s + Math.abs(x - xs[i]), 0) / (xs.length - 1);

function caught(fn: () => unknown): unknown {
  try {
    fn();
    return null;
  } catch (err) {
    return err;
  }
}

describe('2026: real prices through Sep 20, then the AI prediction (simulated here)', () => {
  it('stands in 2025 real prices a year on, and predicts from real_until_utc on', () => {
    const lastReal = at(REAL_UNTIL_2026) - 900;
    // 2026's "real" part is 2025's price at the same Central-time moment.
    expect(priceAt('LZ_HOUSTON', lastReal)).toBe(priceAt('LZ_HOUSTON', lastReal - 365 * 86_400));
    expect(isPredicted(lastReal)).toBe(false);
    expect(isPredicted(at(REAL_UNTIL_2026))).toBe(true);
    // A plausible p50: within the range of the week before, and smoother than the real prices.
    const week = Array.from({ length: 7 * 96 }, (_, k) => priceAt('LZ_NORTH', at(REAL_UNTIL_2026) - (k + 1) * 900) as number);
    const day = Array.from({ length: 96 }, (_, k) => priceAt('LZ_NORTH', at(REAL_UNTIL_2026) + k * 900) as number);
    expect(day.every((p) => p >= Math.min(...week) && p <= Math.max(...week))).toBe(true);
    expect(roughness(day)).toBeLessThan(roughness(week.slice(0, 96)));
  });

  it('marks ticks after real_until_utc as predicted, and says so once with prediction_started', () => {
    const { sim, step } = running(10, 'predicted_2026');
    const ticks: Tick[] = [];
    const kinds: string[] = [];
    for (let i = 0; i < 60 && sim.status !== 'finished'; i++) {
      const f = frame(step());
      ticks.push(...f.ticks);
      kinds.push(...f.events.map((e) => e.kind));
    }
    expect(sim.settings.year).toBe(2026);
    const first = ticks.findIndex((t) => t.predicted);
    expect(ticks[first].ts_utc).toBe(REAL_UNTIL_2026);
    expect(ticks.slice(0, first).every((t) => t.predicted === false)).toBe(true);
    expect(ticks.slice(first).every((t) => t.predicted === true)).toBe(true);
    expect(kinds.filter((k) => k === 'prediction_started')).toHaveLength(1);
    const started = sim.snapshot().events.find((e) => e.kind === 'prediction_started');
    expect(started?.sim_utc).toBe(REAL_UNTIL_2026);
    expect(started?.message).toMatch(/prediction, not real ERCOT data/);
  });

  it('picks a year’s preset, and refuses a preset from another year', () => {
    expect(new MockSim().start({ year: 2026 }).settings).toMatchObject({ year: 2026, preset: 'jan1_2026' });
    expect(new MockSim().start({ year: 2025 }).settings).toMatchObject({ year: 2025, preset: 'morning_spike' });
    expect(new MockSim().start({ preset: 'jan1_2026' }).settings.year).toBe(2026);
    expect(caught(() => new MockSim().start({ year: 2026, preset: 'summer_spike' }))).toMatchObject({ status: 400, code: 'invalid_preset' });
    expect(caught(() => new MockSim().start({ year: 2024 }))).toMatchObject({ status: 400, code: 'invalid_year' });
    // Both "From January 1" presets replay.
    for (const preset of ['jan1_2025', 'jan1_2026']) {
      const { sim, step } = running(2, preset);
      step();
      expect(sim.snapshot().history[0].ts_utc).toBe(PRESETS.find((p) => p.id === preset)?.start_utc);
    }
  });
});

describe('race.potential (all batteries online)', () => {
  it('is hidden until an outage, then shows what the outages cost', () => {
    const { sim, step } = running(10, 'summer_spike');
    for (let i = 0; i < 4; i++) step();
    const before = frame(step()).race;
    expect(before?.shows_potential).toBe(false);
    expect(before?.potential).toMatchObject({ label: 'AI forecast, all batteries online' });
    sim.setAvailability({ scope: 'location', location: 'LZ_NORTH', offline_fraction: 0.5 });
    let f = frame(step());
    for (let i = 0; i < 20; i++) f = frame(step());
    expect(f.race?.shows_potential).toBe(true);
    const p = f.race?.potential;
    expect(p?.shortfall_usd).toBeCloseTo((p?.revenue_usd ?? 0) - f.totals.revenue_usd, 3);
    expect(p?.shortfall_usd).toBeGreaterThan(0); // half of Dallas–Fort Worth missed the summer spike
    const byCity = LOCS.reduce((s, l) => s + (p?.by_location?.[l]?.revenue_usd ?? 0), 0);
    expect(byCity).toBeCloseTo(p?.revenue_usd ?? 0, 2);
    // It stays on for the run, even with every battery back.
    sim.setAvailability({ scope: 'all', available: true });
    expect(frame(step()).race?.shows_potential).toBe(true);
  });

  it('turns on with a controller crash too', () => {
    const { sim, step } = running(1);
    expect(frame(step()).race?.shows_potential).toBe(false);
    sim.kill(4);
    expect(frame(step()).race?.shows_potential).toBe(true);
  });
});

describe('battery_status while paused (docs/API.md: within one frame)', () => {
  it('shows offline batteries and a crashed controller at once, then the hand-over', () => {
    const { sim, step, advance } = running(1);
    step();
    sim.pause();
    step();
    sim.setAvailability({ scope: 'worker', worker_id: 12, available: false }); // Dallas–Fort Worth, b12000-b12999
    const offline = codesOf(frame(step()));
    expect(offline[12_000]).toBe(OFFLINE);
    expect(countCodes(offline, null, null)[OFFLINE]).toBe(1000);

    sim.kill(3); // Houston, b03000-b03999
    const down = codesOf(frame(step()));
    expect(down[3_000]).toBe(DOWN);
    expect(countCodes(down, null, null)[DOWN]).toBe(1000);
    // Still paused: handed over once the heartbeat runs out (6 s), and back to their own look.
    advance(6_000);
    const back = codesOf(frame(step()));
    expect(countCodes(back, null, null)[DOWN]).toBe(0);
    expect(back[12_000]).toBe(OFFLINE);
  });
});

describe('POST /chaos/kill over HTTP: public, one crash every 2 s', () => {
  it('needs no passcode, and answers 429 with Retry-After inside 2 s', async () => {
    const api = createMockApi({ basePath: '', frameMs: 60_000 });
    const server = createServer((req, res) => void api.handle(req, res));
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address() as AddressInfo;
    const base = `http://127.0.0.1:${port}`;
    try {
      expect((await fetch(`${base}/run/start`, { method: 'POST', body: '{}' })).status).toBe(202);
      await new Promise((resolve) => setTimeout(resolve, 1600)); // past the "starting" phase
      api.sim.stepWall();
      const first = await fetch(`${base}/chaos/kill?worker_id=6`, { method: 'POST' });
      expect(first.status).toBe(200);
      expect(await first.json()).toMatchObject({ worker_id: 6 });
      const second = await fetch(`${base}/chaos/kill`, { method: 'POST' });
      expect(second.status).toBe(429);
      expect(second.headers.get('retry-after')).toBe('2');
      expect(await second.json()).toMatchObject({ error: 'rate_limited', retry_after_s: 2 });
      const preflight = await fetch(`${base}/chaos/kill`, { method: 'OPTIONS' });
      expect(preflight.headers.get('access-control-allow-headers')).toBe('Content-Type');
      expect((await fetch(`${base}/presenter/check`, { method: 'POST' })).status).toBe(404);
    } finally {
      api.close();
      await new Promise((resolve) => server.close(resolve));
    }
  });
});
