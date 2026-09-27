// The mock's failure model (docs/API.md, the human's rules): each worker's charge_level and usable_charge, a
// crash that empties a controller's batteries and stays down, a user restart (POST /workers/{id}/restart) that
// takes them back as they are, and a controller taken offline that loses half its usable charge.

import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { describe, expect, it } from 'vitest';
import type { FrameMessage, Worker } from '../src/api/types.ts';
import { createMockApi } from './api.ts';
import { MockSim } from './sim.ts';

function running(speed = 2, preset?: string) {
  let now = Date.parse('2026-09-25T12:00:00Z');
  const sim = new MockSim({ now: () => now, random: () => 0.5 });
  sim.start({ speed_tps: speed, preset: preset ?? 'ai_best_evening' }); // May 30, 2025, from 6 pm
  const step = () => {
    now += 1000;
    return sim.stepWall() as FrameMessage;
  };
  step();
  step(); // past the 1.5 s "starting" phase
  return { sim, step, advance: (ms: number) => (now += ms) };
}

const byId = (workers: Worker[], id: number) => workers.find((w) => w.worker_id === id) as Worker;

function caught(fn: () => unknown): unknown {
  try {
    fn();
    return null;
  } catch (err) {
    return err;
  }
}

describe('each controller’s charge (Worker.charge_level and usable_charge)', () => {
  it('is the capacity-weighted charge of the batteries it runs, and the usable share above the 20% reserve', () => {
    const { step } = running(1);
    const f = step();
    for (const w of f.workers) {
      expect(w.charge_level).toBeGreaterThanOrEqual(0.2);
      expect(w.charge_level).toBeLessThanOrEqual(1);
      expect(w.usable_charge).toBeCloseTo(((w.charge_level as number) - 0.2) / 0.8, 3);
    }
    // Every battery starts 90% charged: 87.5% of the usable energy.
    expect(new MockSim().snapshot().workers[0]).toMatchObject({ charge_level: 0.9, usable_charge: 0.875 });
  });
});

describe('a crash empties the controller’s batteries, and nothing comes back on its own', () => {
  it('drops to 0 at once, hands the empty batteries over (the others dip), and stays down', () => {
    const calm = running(1);
    const { sim, step } = running(1);
    calm.step();
    step();
    sim.kill(6);
    expect(byId(sim.snapshot().workers, 6)).toMatchObject({ usable_charge: 0, charge_level: 0.2 });
    step(); // not responding
    const after = step(); // crashed and handed over
    const calmNow = calm.step().workers;
    calm.step();
    expect(byId(after.workers, 6)).toMatchObject({ status: 'dead', batteries: 0, usable_charge: null, charge_level: null });
    // The adopters now run some empty batteries: their charge dips below the calm run's.
    expect(byId(after.workers, 0).usable_charge).toBeLessThan(byId(calmNow, 0).usable_charge as number);
    // Nothing restarts it: still down many steps later.
    let f = after;
    for (let i = 0; i < 30; i++) f = step();
    expect(byId(f.workers, 6).status).toBe('dead');
    expect(f.events.some((e) => e.kind === 'worker_recovered')).toBe(false);
  });

  it('restarts only on request, taking its own batteries back with whatever charge they have', () => {
    const { sim, step } = running(1);
    expect(caught(() => sim.restart(6))).toMatchObject({ status: 409, code: 'not_crashed' });
    expect(caught(() => sim.restart(25))).toMatchObject({ status: 404, code: 'unknown_worker' });
    sim.kill(6);
    step();
    step();
    for (let i = 0; i < 6; i++) step(); // the other controllers run its batteries meanwhile
    expect(sim.restart(6)).toEqual({ worker_id: 6, status: 'starting' });
    expect(caught(() => sim.restart(6))).toMatchObject({ status: 409, code: 'not_crashed' }); // already restarting
    expect(byId(sim.snapshot().workers, 6).status).toBe('starting');
    const back = step();
    const w = byId(back.workers, 6);
    expect(w).toMatchObject({ status: 'healthy', batteries: 1000, by_location: { LZ_HOUSTON: 1000 }, adopted_from: [] });
    expect(w.usable_charge).toBeGreaterThanOrEqual(0);
    expect(w.usable_charge).toBeLessThan(0.3); // emptied, then run by the others for a few steps
    expect(back.workers.every((x) => !x.adopted_from.includes(6))).toBe(true);
    const recovered = back.events.find((e) => e.kind === 'worker_recovered');
    expect(recovered?.detail).toMatchObject({ worker_id: 6, controller: 7, batteries: 1000, by_location: { LZ_HOUSTON: 1000 } });
    expect(recovered?.message).toMatch(/^Controller 7 is back and has taken back its 1,000 batteries in Houston, at \d+% usable charge\.$/);
  });

  it('restarts while paused too, a second later', () => {
    const { sim, step, advance } = running(1);
    sim.pause();
    sim.kill(2);
    advance(6_000);
    step(); // handed over while paused
    sim.restart(2);
    expect(byId(step().workers, 2).status).toBe('healthy'); // a second has passed
  });
});

describe('a controller taken offline loses half its usable charge', () => {
  it('halves at that moment (in the race too), holds still while offline, and carries on from there', () => {
    const { sim, step } = running(1);
    const before = byId(step().workers, 12).usable_charge as number;
    const raceBefore = sim.snapshot().race?.by_location.LZ_NORTH.charge_level as number;
    sim.setAvailability({ scope: 'worker', worker_id: 12, available: false });
    const halved = byId(sim.snapshot().workers, 12).usable_charge as number;
    expect(halved).toBeCloseTo(before / 2, 3);
    // The race's Dallas–Fort Worth (8,000 batteries) lost 1,000 batteries' half-charge too.
    expect(sim.snapshot().race?.by_location.LZ_NORTH.charge_level).toBeLessThan(raceBefore);
    for (let i = 0; i < 5; i++) step();
    expect(byId(sim.snapshot().workers, 12).usable_charge).toBeCloseTo(halved, 6);
    // Going offline again does not halve it twice; back online it moves on from the halved level.
    sim.setAvailability({ scope: 'worker', worker_id: 12, available: false });
    expect(byId(sim.snapshot().workers, 12).usable_charge).toBeCloseTo(halved, 6);
    sim.setAvailability({ scope: 'worker', worker_id: 12, available: true });
    expect(byId(sim.snapshot().workers, 12).usable_charge).toBeCloseTo(halved, 6);
    // City and single-battery offline change no charge.
    const houston = byId(sim.snapshot().workers, 0).usable_charge;
    sim.setAvailability({ scope: 'location', location: 'LZ_HOUSTON', offline_fraction: 0.5 });
    expect(byId(sim.snapshot().workers, 0).usable_charge).toBe(houston);
  });
});

describe('POST /workers/{worker_id}/restart over HTTP', () => {
  it('is public, answers 409 not_crashed for a working controller, and 429 inside 2 s', async () => {
    const api = createMockApi({ basePath: '', frameMs: 60_000 });
    const server = createServer((req, res) => void api.handle(req, res));
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    const { port } = server.address() as AddressInfo;
    const base = `http://127.0.0.1:${port}`;
    try {
      expect((await fetch(`${base}/run/start`, { method: 'POST', body: '{}' })).status).toBe(202);
      await new Promise((resolve) => setTimeout(resolve, 1600)); // past the "starting" phase
      api.sim.stepWall();
      const up = await fetch(`${base}/workers/3/restart`, { method: 'POST' });
      expect(up.status).toBe(409);
      expect(await up.json()).toMatchObject({ error: 'not_crashed' });
      const soon = await fetch(`${base}/workers/3/restart`, { method: 'POST' });
      expect(soon.status).toBe(429);
      expect(soon.headers.get('retry-after')).toBe('2');
    } finally {
      api.close();
      await new Promise((resolve) => server.close(resolve));
    }
  });
});
