import { describe, expect, it } from 'vitest';
import type { Tick, TickAction } from '../../../api/types.ts';
import { buildTrack, indexToX, priceScale, TICK_MS, timeToX, trackAction, trackPrice, xToIndex } from './timeline.ts';

const T0 = Date.parse('2025-07-11T23:00:00Z'); // 6 pm Central time

function tick(i: number, price: number, fleetKw: number, predicted = false): Tick {
  const ts = new Date(T0 + i * 300_000).toISOString().replace('.000Z', 'Z');
  const action: TickAction = fleetKw > 0 ? 'sell' : fleetKw < 0 ? 'charge' : 'hold';
  return {
    ts_utc: ts,
    // Central daylight time: the first 72 ticks are Jul 11, then Jul 12.
    ts_local: new Date(T0 + i * 300_000 - 5 * 3_600_000).toISOString().slice(0, 19) + '-05:00',
    price: { LZ_HOUSTON: price, LZ_NORTH: price + 10 },
    action: { LZ_HOUSTON: action, LZ_NORTH: action },
    target_kw: { fleet: fleetKw },
    actual_kw: { LZ_HOUSTON: fleetKw / 2, LZ_NORTH: fleetKw / 2, fleet: fleetKw },
    error_pct: 0,
    revenue_usd: { fleet: 0 },
    charge_level: { fleet: 0.5 },
    healthy_workers: 20,
    predicted,
  };
}

/** 6 pm Jul 11 to 6 pm Jul 12: waiting, a spike sold into at 9 pm, cheap charging overnight, predicted from noon. */
const history: Tick[] = Array.from({ length: 288 }, (_, i) => {
  const price = i === 36 ? 1750 : i >= 90 && i < 120 ? 15 : 40 + (i % 12);
  const kw = i >= 30 && i < 42 ? 50_000 : i >= 90 && i < 120 ? -30_000 : 0;
  return tick(i, price, kw, i >= 216);
});

const weights = { LZ_HOUSTON: 7000, LZ_NORTH: 8000 };

describe('the track’s price and action', () => {
  it('follows the selected city, or the fleet’s battery-weighted average', () => {
    const t = history[0];
    expect(trackPrice(t, 'LZ_HOUSTON', weights)).toBe(40);
    expect(trackPrice(t, null, weights)).toBeCloseTo((40 * 7000 + 50 * 8000) / 15_000, 9);
    expect(trackPrice(t, null, {})).toBe(45);
    expect(trackPrice(t, 'LZ_AEN', weights)).toBeNull();
  });

  it('is selling or buying by the power delivered, else by the actions', () => {
    expect(trackAction(history[35], null)).toBe('sell');
    expect(trackAction(history[100], 'LZ_NORTH')).toBe('buy');
    expect(trackAction(history[0], null)).toBeNull();
    const noPower = { ...history[35], actual_kw: undefined } as unknown as Tick;
    expect(trackAction(noPower, null)).toBe('sell');
  });

  it('compresses spikes, so an ordinary day still shows next to one', () => {
    expect(priceScale(0)).toBe(0);
    expect(priceScale(40)).toBeCloseTo(Math.asinh(1), 9);
    expect(priceScale(1750) / priceScale(40)).toBeLessThan(6); // not 44×
    expect(priceScale(-20)).toBeLessThan(0);
  });
});

describe('buildTrack', () => {
  const track = buildTrack(history, null, weights, 270, 30);
  if (!track) throw new Error('no track');

  it('spans the run so far, each tick its 5-minute slot', () => {
    expect(track.t0).toBe(T0);
    expect(track.t1).toBe(T0 + 288 * TICK_MS);
    expect(track.count).toBe(288);
    expect(track.path.startsWith('M')).toBe(true);
    expect(timeToX(track, T0)).toBe(0);
    expect(timeToX(track, track.t1)).toBe(270);
  });

  it('shades green where the fleet sold and blue where it bought', () => {
    expect(track.bands.map((b) => b.kind)).toEqual(['sell', 'buy']);
    const [sell, buy] = track.bands;
    expect(sell.x0).toBeCloseTo(timeToX(track, T0 + 30 * TICK_MS), 9);
    expect(sell.x1).toBeCloseTo(timeToX(track, T0 + 42 * TICK_MS), 9);
    expect(buy.x0).toBeCloseTo(timeToX(track, T0 + 90 * TICK_MS), 9);
  });

  it('marks each day’s high ▲ and low ▼', () => {
    const marks = track.markers.map((m) => [m.kind, m.index, m.price]);
    expect(marks).toContainEqual(['high', 36, 1750 * (7000 / 15_000) + 1760 * (8000 / 15_000)]);
    expect(marks.filter(([k]) => k === 'low').map(([, i]) => i)).toContain(90);
    // Highs sit above the line, lows below it.
    for (const m of track.markers) expect(m.y).toBeGreaterThanOrEqual(0);
    expect(track.markers.length).toBeLessThanOrEqual(4);
  });

  it('shades where the AI’s prediction takes over', () => {
    expect(track.predictedX).toBeCloseTo(timeToX(track, T0 + 216 * TICK_MS), 9);
    expect(buildTrack(history.slice(0, 200), null, weights, 270, 30)?.predictedX).toBeNull();
  });

  it('turns a pointer position into a tick, and a tick into its place', () => {
    for (const i of [0, 36, 150, 287]) expect(xToIndex(track, history, indexToX(track, history, i))).toBe(i);
    expect(xToIndex(track, history, -50)).toBe(0);
    expect(xToIndex(track, history, 9999)).toBe(287);
    expect(indexToX(track, history, 287)).toBeLessThan(270); // the live tick is visible, inside the track
  });

  it('draws at most a few points per pixel, whatever the run length', () => {
    const long = Array.from({ length: 2000 }, (_, i) => tick(i, 40 + (i % 7), 0));
    const t = buildTrack(long, 'LZ_HOUSTON', weights, 200, 30);
    const points = (t?.path.match(/[ML]/g) ?? []).length;
    expect(points).toBeLessThanOrEqual(200 * 4 + 4);
    expect(buildTrack([], null, weights, 270, 30)).toBeNull();
    expect(buildTrack(history, null, weights, 0, 30)).toBeNull();
  });
});
