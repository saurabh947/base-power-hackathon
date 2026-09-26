import { describe, expect, it } from 'vitest';
import type { Worker } from '../../../api/types.ts';
import { buildGauges, fmtCharge, gaugeAction, gaugeChanges, gaugeWords, summarize, usableCharge, type Gauge } from './gauges.ts';

function worker(id: number, patch: Partial<Worker> = {}): Worker {
  return {
    worker_id: id,
    status: 'healthy',
    pid: 100 + id,
    batteries: 1000,
    last_tick_utc: null,
    adopted_from: [],
    by_location: id < 7 ? { LZ_HOUSTON: 1000 } : { LZ_NORTH: 1000 },
    charge_level: 0.62,
    usable_charge: 0.525,
    ...patch,
  };
}

const one = (w: Worker, scope: string | null = null, offline: number[] = []): Gauge => buildGauges([w], scope, offline)[0];
const eighteen = (gauges: Gauge[]) => gauges.find((g) => g.number === 18) as Gauge;
/** Controller 4 as `patch` says, as a one-gauge list. */
const at = (patch: Partial<Worker>, offline: number[] = []) => buildGauges([worker(3, patch)], null, offline);

describe('the gauge fill: usable charge above the 20% reserve', () => {
  it('fills to usable_charge, 0 to 100%', () => {
    expect(one(worker(0, { usable_charge: 0.525 }))).toMatchObject({ fill: 0.525, value: '53%', look: 'working', number: 1 });
    expect(one(worker(0, { usable_charge: 0 })).fill).toBe(0);
    expect(one(worker(0, { usable_charge: 1 }))).toMatchObject({ fill: 1, value: '100%' });
    // Out-of-range values stay inside the gauge.
    expect(one(worker(0, { usable_charge: 1.2 })).fill).toBe(1);
    expect(one(worker(0, { usable_charge: -0.1 })).fill).toBe(0);
  });

  it('works it out from charge_level when usable_charge is missing (an older backend): the reserve is empty', () => {
    expect(usableCharge({ charge_level: 0.62 })).toBeCloseTo(0.525, 10);
    expect(usableCharge({ charge_level: 0.2 })).toBe(0);
    expect(usableCharge({ charge_level: 1 })).toBe(1);
    expect(one(worker(0, { usable_charge: undefined, charge_level: 0.6 })).fill).toBeCloseTo(0.5, 10);
  });

  it('says "—" when a controller runs no batteries, and knows nothing when neither field is there', () => {
    expect(usableCharge({ usable_charge: null, charge_level: null })).toBeNull();
    expect(usableCharge({})).toBeUndefined();
    expect(fmtCharge(null)).toBe('—');
    expect(fmtCharge(undefined)).toBeNull();
    expect(fmtCharge(0.004)).toBe('0%');
    const unknown = one(worker(0, { usable_charge: undefined, charge_level: undefined }));
    expect(unknown).toMatchObject({ fill: null, value: null });
    expect(summarize([unknown]).known).toBe(false);
  });
});

describe('a crashed controller', () => {
  it('drops to 0% with its batteries emptied, and shows "—" once they were handed over', () => {
    const emptied = one(worker(6, { status: 'dead', usable_charge: 0, charge_level: 0.2 }));
    expect(emptied).toMatchObject({ look: 'crashed', fill: 0, value: '0%' });
    const handedOver = one(worker(6, { status: 'dead', batteries: 0, by_location: {}, usable_charge: null, charge_level: null }));
    expect(handedOver).toMatchObject({ look: 'crashed', fill: 0, value: '—' });
    // Whatever charge a backend still reports, a crashed gauge is empty.
    expect(one(worker(6, { status: 'dead', usable_charge: 0.4 }))).toMatchObject({ fill: 0, value: '0%' });
    expect(gaugeWords(handedOver, null)).toEqual({
      title: 'Controller 7 · —',
      detail: 'Crashed. Its batteries were emptied and handed to the other controllers.',
    });
  });

  it('is offered a restart, a working one a crash, as long as a run is live', () => {
    const live = { past: false, running: true, working: 19 };
    const crashed = one(worker(6, { status: 'dead', usable_charge: null }));
    expect(gaugeAction(crashed, live)).toEqual({ action: 'restart', hint: 'Click to restart it' });
    expect(gaugeAction(one(worker(3)), live)).toEqual({ action: 'crash', hint: 'Click to crash it' });
    expect(gaugeAction(one(worker(3)), { ...live, working: 2 }).action).toBeNull();
    expect(gaugeAction(one(worker(3, { status: 'suspect' })), live).action).toBeNull();
    expect(gaugeAction(one(worker(3, { status: 'starting' })), live).action).toBeNull();
    expect(gaugeAction(crashed, { ...live, past: true })).toEqual({ action: null, hint: 'Go back to live to crash or restart a controller' });
    expect(gaugeAction(crashed, { ...live, running: false }).hint).toBe('Start a simulation first');
  });
});

describe('a controller taken offline', () => {
  it('keeps a ghost mark where it was: twice today’s level, since going offline halves it', () => {
    const g = one(worker(2, { usable_charge: 0.3 }), null, [2]);
    expect(g).toMatchObject({ offline: true, fill: 0.3, ghost: 0.6, value: '30%' });
    expect(one(worker(2, { usable_charge: 0.7 }), null, [2]).ghost).toBe(1);
    expect(one(worker(2, { usable_charge: 0 }), null, [2]).ghost).toBeNull(); // nothing to mark
    expect(one(worker(2, { usable_charge: 0.3 })).ghost).toBeNull(); // online
    expect(gaugeWords(g, null).detail).toBe(
      'Its 1,000 batteries in Houston are offline: half their charge was lost (it had 60%), and the rest holds still.',
    );
    // Empty already: nothing was lost, so nothing is marked or said about it.
    expect(gaugeWords(one(worker(2, { usable_charge: 0 }), null, [2]), null).detail).toBe('Its 1,000 batteries in Houston are offline and hold still.');
  });
});

describe('selecting a city highlights the controllers serving it (workers[].by_location)', () => {
  const workers = [
    worker(0),
    worker(7),
    worker(17, { by_location: { LZ_CPS: 600, LZ_AEN: 400 } }),
    worker(4, { status: 'dead', batteries: 0, by_location: {}, usable_charge: null }),
  ];

  it('marks who serves the city, counting only its batteries there', () => {
    const houston = buildGauges(workers, 'LZ_HOUSTON');
    expect(houston.map((g) => [g.number, g.serving, g.batteries])).toEqual([
      [1, true, 1000],
      [5, false, 0],
      [8, false, 0],
      [18, false, 0],
    ]);
    expect(buildGauges(workers, 'LZ_AEN').find((g) => g.number === 18)).toMatchObject({ serving: true, batteries: 400 });
    expect(summarize(buildGauges(workers, 'LZ_CPS')).serving).toBe(1);
  });

  it('highlights nothing for all of Texas', () => {
    const all = buildGauges(workers, null);
    expect(all.every((g) => g.serving === null)).toBe(true);
    expect(summarize(all)).toMatchObject({ total: 4, working: 3, crashed: 1, serving: null, known: true });
    expect(gaugeWords(eighteen(all), null).detail).toBe('Working, running 1,000 batteries in San Antonio and Austin.');
    expect(gaugeWords(eighteen(buildGauges(workers, 'LZ_AEN')), 'LZ_AEN').detail).toBe('Working, running 400 batteries in Austin.');
  });
});

describe('sudden changes, animated', () => {
  it('drops on a crash and when its batteries go offline, but never on ordinary trading', () => {
    expect(gaugeChanges(at({ usable_charge: 0.5 }), at({ status: 'suspect', usable_charge: 0 })).get(3)).toEqual({ kind: 'drop', was: 0.5 });
    expect(gaugeChanges(at({ usable_charge: 0.5 }), at({ status: 'dead', usable_charge: null })).get(3)).toEqual({ kind: 'drop', was: 0.5 });
    expect(gaugeChanges(at({ usable_charge: 0.08 }), at({ usable_charge: 0.04 }, [3])).get(3)).toEqual({ kind: 'drop', was: 0.08 });
    // At 10 steps a second a frame can sell or charge 40% of a battery: that is trading, not a drop.
    expect(gaugeChanges(at({ usable_charge: 0.5 }), at({ usable_charge: 0.1 })).size).toBe(0);
    expect(gaugeChanges(at({ usable_charge: 0.1 }), at({ usable_charge: 0.5 })).size).toBe(0);
    // An empty controller that crashes has nothing to lose: no drop to show.
    expect(gaugeChanges(at({ usable_charge: 0 }), at({ status: 'dead', usable_charge: null })).size).toBe(0);
  });

  it('drops from where it stood lately when the batteries emptied a frame before the crash was reported', () => {
    const emptied = at({ usable_charge: 0 }); // paused: emptied at the kill, still "working" for a moment
    expect(gaugeChanges(emptied, at({ status: 'suspect', usable_charge: 0 }), new Map([[3, 0.6]])).get(3)).toEqual({ kind: 'drop', was: 0.6 });
  });

  it('refills when a restarted controller takes its batteries back', () => {
    const dead = at({ status: 'dead', batteries: 0, usable_charge: null });
    const back = at({ usable_charge: 0.22 });
    expect(gaugeChanges(dead, back).get(3)).toEqual({ kind: 'refill', was: 0 });
    expect(gaugeChanges(at({ status: 'starting', usable_charge: null }), back).get(3)?.kind).toBe('refill');
    // The start of a run is not a refill: the charge does not jump.
    expect(gaugeChanges(at({ status: 'starting', usable_charge: 0.8 }), at({ usable_charge: 0.8 })).size).toBe(0);
  });

  it('never animates unknown charge', () => {
    expect(gaugeChanges(at({ usable_charge: undefined, charge_level: undefined }), at({ usable_charge: 0 })).size).toBe(0);
  });
});
