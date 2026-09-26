import { describe, expect, it } from 'vitest';
import type { Cells } from './cells.ts';
import { CODES } from './status.ts';
import { allocTargets, computeTargets, darkness, DIM_ALPHA, PALETTE } from './style.ts';

/** Two cells of 40 batteries each: cell 0 in city 0, cell 1 in city 1. */
function cells(): Cells {
  return {
    count: 2,
    spacing: 6,
    x: Float32Array.from([10, 30]),
    y: Float32Array.from([10, 10]),
    n: Uint32Array.from([40, 40]),
    city: Uint8Array.from([0, 1]),
    cellOf: new Int32Array(80),
    maxN: 40,
    refN: 40,
  };
}

function counts(...perCell: [number, number, number, number, number][]): Uint32Array {
  const out = new Uint32Array(perCell.length * CODES);
  perCell.forEach((c, i) => out.set(c, i * CODES));
  return out;
}

const look = (c: [number, number, number, number, number], options = { selected: null as number | null, reducedMotion: false }) => {
  const out = allocTargets(2);
  computeTargets(cells(), counts(c, c), options, out);
  return out;
};

describe('dot looks (docs/UX.md, Live map)', () => {
  it('waiting is medium green; selling is bright green with a glow; charging has a light ring', () => {
    const waiting = look([0, 40, 0, 0, 0]);
    expect([waiting.red[0], waiting.green[0], waiting.blue[0]]).toEqual([...PALETTE.waiting]);
    expect(waiting.glow[0]).toBe(0);
    expect(waiting.ring[0]).toBe(0);

    const selling = look([0, 0, 40, 0, 0]);
    expect([selling.red[0], selling.green[0], selling.blue[0]]).toEqual([...PALETTE.selling]);
    expect(selling.green[0]).toBeGreaterThan(waiting.green[0]); // brighter while selling
    expect(selling.glow[0]).toBe(1);

    const charging = look([0, 0, 0, 40, 0]);
    expect(charging.ring[0]).toBe(1);
    expect(charging.glow[0]).toBe(0);
  });

  it('offline batteries pull the dot towards dim gray, in proportion', () => {
    const all = look([40, 0, 0, 0, 0]);
    expect([all.red[0], all.green[0], all.blue[0]]).toEqual([...PALETTE.offline]);
    const quarter = look([10, 0, 30, 0, 0]);
    expect(quarter.green[0]).toBeCloseTo(PALETTE.selling[1] + (PALETTE.offline[1] - PALETTE.selling[1]) * 0.25, 5);
    expect(quarter.glow[0]).toBeCloseTo(0.75, 5);
  });

  it('a dot with batteries whose controller is down goes dark, more so the more of them', () => {
    expect(darkness(0, 40)).toBe(0);
    expect(darkness(1, 40)).toBeGreaterThanOrEqual(0.5);
    expect(darkness(40, 40)).toBe(1);
    expect(darkness(20, 40)).toBeGreaterThan(darkness(4, 40));
    const flashing = look([0, 0, 30, 0, 10]);
    expect(flashing.dark[0]).toBeCloseTo(darkness(10, 40), 5);
    // Green underneath: that is what comes back when the batteries are handed over.
    const allDown = look([0, 0, 0, 0, 40]);
    expect([allDown.red[0], allDown.green[0], allDown.blue[0]]).toEqual([...PALETTE.waiting]);
  });

  it('dims other cities a little while one is selected, and drops glows for reduced motion', () => {
    const selected = look([0, 0, 40, 0, 0], { selected: 1, reducedMotion: false });
    expect(selected.alpha[1]).toBe(1);
    expect(selected.alpha[0]).toBeCloseTo(DIM_ALPHA, 5);
    expect(selected.glow[0]).toBeLessThan(selected.glow[1]);
    const calm = look([0, 0, 40, 0, 0], { selected: null, reducedMotion: true });
    expect(calm.glow[0]).toBe(0);
    expect(calm.alpha[0]).toBe(1);
  });
});
