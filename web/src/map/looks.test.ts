import { describe, expect, it } from 'vitest';
import type { Cells } from './cells.ts';
import { allocLooks, cellLook, computeLooks, DIM_ALPHA, edgeAlpha, edgeWidth, majority, PALETTE, RING_MIN_RADIUS, WAITING_SCALE } from './looks.ts';
import { CHARGING, CODES, DOWN, OFFLINE, SELLING, WAITING } from './status.ts';

/** Two cells of 40 batteries each: cell 0 in city 0, cell 1 in city 1. */
function cells(): Cells {
  return {
    count: 2,
    spacing: 8,
    x: Float32Array.from([10, 30]),
    y: Float32Array.from([10, 10]),
    n: Uint32Array.from([40, 40]),
    city: Uint8Array.from([0, 1]),
    cellOf: new Int32Array(80),
    maxN: 40,
    refN: 40,
  };
}

type Counts = [offline: number, waiting: number, selling: number, charging: number, down: number];

function counts(...perCell: Counts[]): Uint32Array {
  const out = new Uint32Array(perCell.length * CODES);
  perCell.forEach((c, i) => out.set(c, i * CODES));
  return out;
}

const options = { selected: null as number | null, reducedMotion: false };
const look = (c: Counts, o = options) => cellLook(cells(), counts(c, c), 0, o);

describe('dot looks (docs/UX.md, Live map v2): one meaning each, shape as well as colour', () => {
  it('selling is green and solid, with a halo', () => {
    const l = look([0, 0, 40, 0, 0]);
    expect(l).toMatchObject({ code: SELLING, color: PALETTE.selling, fill: 1, ring: 0, edge: 0, crash: 0, glow: 1 });
  });

  it('charging is blue and solid, without the halo', () => {
    expect(look([0, 0, 0, 40, 0])).toMatchObject({ code: CHARGING, color: PALETTE.charging, fill: 1, ring: 0, glow: 0 });
    expect(PALETTE.charging[2]).toBeGreaterThan(PALETTE.charging[1]); // blue, not green
  });

  it('waiting is gray and small', () => {
    const l = look([0, 40, 0, 0, 0]);
    expect(l).toMatchObject({ code: WAITING, color: PALETTE.waiting, fill: 1, glow: 0 });
    expect(l.radius).toBeCloseTo(look([0, 0, 40, 0, 0]).radius * WAITING_SCALE, 5);
  });

  it('offline is a hollow red ring', () => {
    const l = look([40, 0, 0, 0, 0]);
    expect(l).toMatchObject({ code: OFFLINE, color: PALETTE.outage, fill: 0, ring: 1, edge: 0 });
    expect(l.radius).toBeGreaterThanOrEqual(RING_MIN_RADIUS);
  });

  it('a crashed controller’s batteries are solid red and pulse (crash), until handed over', () => {
    const l = look([0, 0, 0, 0, 40]);
    expect(l).toMatchObject({ code: DOWN, color: PALETTE.outage, fill: 1, crash: 1, down: 1 });
    // Handed over: the same batteries back to their own look.
    expect(look([0, 0, 40, 0, 0]).crash).toBe(0);
  });
});

describe('aggregated dots: the majority look, and a red edge for any outage', () => {
  it('take the look of most of their batteries', () => {
    expect(majority(counts([0, 5, 30, 5, 0]))).toBe(SELLING);
    expect(majority(counts([25, 5, 5, 5, 0]))).toBe(OFFLINE);
    expect(majority(counts([0, 10, 0, 30, 0]))).toBe(CHARGING);
    expect(majority(counts([0, 0, 0, 0, 0]))).toBe(-1);
  });

  it('break ties in favour of the outage, so it is never hidden', () => {
    expect(majority(counts([20, 0, 20, 0, 0]))).toBe(OFFLINE);
    expect(majority(counts([0, 0, 20, 0, 20]))).toBe(DOWN);
    expect(majority(counts([10, 0, 0, 0, 10]))).toBe(DOWN);
    expect(majority(counts([0, 0, 20, 20, 0]))).toBe(SELLING);
  });

  it('give a dot with ANY offline or crashed batteries a red edge, stronger with the share', () => {
    const one = look([1, 0, 39, 0, 0]);
    expect(one.code).toBe(SELLING); // still a green dot…
    expect(one.edge).toBeCloseTo(1 / 40, 9); // …with a red edge
    expect(look([0, 0, 38, 0, 2]).edge).toBeCloseTo(2 / 40, 9);
    expect(look([12, 0, 28, 0, 0]).edge).toBeGreaterThan(one.edge);
    expect(look([0, 0, 40, 0, 0]).edge).toBe(0);
    // The edge is always visible, and never over-thick.
    expect(edgeWidth(1 / 40)).toBeGreaterThanOrEqual(1);
    expect(edgeWidth(1)).toBeLessThanOrEqual(2.2);
    expect(edgeAlpha(1 / 40)).toBeGreaterThan(0.4);
    expect(edgeAlpha(0.3)).toBeGreaterThan(edgeAlpha(0.1));
    expect(edgeAlpha(0.6)).toBe(1);
    expect(edgeWidth(0)).toBe(0);
    expect(edgeAlpha(0)).toBe(0);
  });

  it('draw no extra edge on a dot that is itself an outage (the ring or the red dot says it)', () => {
    expect(look([30, 10, 0, 0, 0]).edge).toBe(0);
    expect(look([0, 10, 0, 0, 30]).edge).toBe(0);
  });

  it('pulse the edge while some of a dot’s batteries have lost their controller', () => {
    const l = look([0, 30, 0, 0, 10]);
    expect(l).toMatchObject({ code: WAITING, crash: 0 });
    expect(l.down).toBeCloseTo(0.25, 9);
    expect(l.edge).toBeCloseTo(0.25, 9);
  });
});

describe('the selected city and reduced motion', () => {
  it('dims other cities a little, softening their halo', () => {
    const out = allocLooks(2);
    computeLooks(cells(), counts([0, 0, 40, 0, 0], [0, 0, 40, 0, 0]), { selected: 1, reducedMotion: false }, out);
    expect(out.alpha[1]).toBe(1);
    expect(out.alpha[0]).toBeCloseTo(DIM_ALPHA, 5);
    expect(out.glow[0]).toBeLessThan(out.glow[1]);
  });

  it('drops the halo for prefers-reduced-motion; the shapes stay', () => {
    const calm = look([0, 0, 40, 0, 0], { selected: null, reducedMotion: true });
    expect(calm.glow).toBe(0);
    expect(look([40, 0, 0, 0, 0], { selected: null, reducedMotion: true }).ring).toBe(1);
  });

  it('fills every look array for the renderer', () => {
    const out = allocLooks(2);
    computeLooks(cells(), counts([40, 0, 0, 0, 0], [0, 0, 0, 40, 0]), options, out);
    expect([out.red[0], out.green[0], out.blue[0], out.fill[0], out.ring[0]]).toEqual([...PALETTE.outage, 0, 1]);
    expect([out.red[1], out.green[1], out.blue[1], out.fill[1]]).toEqual([...PALETTE.charging, 1]);
  });
});
