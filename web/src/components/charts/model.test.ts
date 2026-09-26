import { describe, expect, it } from 'vitest';
import type { LiveEvent, Tick } from '../../api/types.ts';
import { actionSpans, buildPowerModel, buildPriceModel, computeXWindow, crashMarkers, MIN_SPAN_SECONDS } from './model.ts';

const S0 = Date.parse('2025-05-16T21:00:00Z') / 1000;
const iso = (s: number) => new Date(s * 1000).toISOString().replace('.000Z', 'Z');

function tick(i: number, action: Tick['action'] = {}, kw?: { target?: number; actual?: number }): Tick {
  return {
    ts_utc: iso(S0 + i * 300),
    ts_local: iso(S0 + i * 300),
    price: { LZ_HOUSTON: 100 + i },
    action,
    target_kw: kw?.target === undefined ? { LZ_HOUSTON: 1000, LZ_NORTH: 500 } : { fleet: kw.target },
    actual_kw: kw?.actual === undefined ? { LZ_HOUSTON: 900, LZ_NORTH: 500 } : { fleet: kw.actual },
    error_pct: 0,
    revenue_usd: {},
    charge_level: {},
    healthy_workers: 20,
  };
}

describe('computeXWindow', () => {
  it('opens at least 6 hours ahead so a new run draws into empty space', () => {
    expect(computeXWindow(S0, S0, null, 6 * 3600)).toEqual({ min: S0, max: S0 + MIN_SPAN_SECONDS });
  });

  it('slides with the replay and stretches to the end of a forecast', () => {
    const now = S0 + 10 * 3600;
    expect(computeXWindow(S0, now, null, 6 * 3600)).toEqual({ min: now - 6 * 3600, max: now + 300 });
    expect(computeXWindow(S0, now, now + 4 * 3600, 6 * 3600)).toEqual({ min: now - 6 * 3600, max: now + 4 * 3600 });
  });

  it('shows the whole replay for "all"', () => {
    const now = S0 + 30 * 3600;
    expect(computeXWindow(S0, now, null, null)).toEqual({ min: S0, max: now + 300 });
  });

  it('returns null with nothing to show', () => {
    expect(computeXWindow(null, null, null, 3600)).toBeNull();
  });
});

describe('buildPriceModel', () => {
  it('merges 5-minute ticks with 15-minute forecast points on one axis', () => {
    const history = [tick(0), tick(1), tick(2), tick(3)];
    const forecast = {
      location: 'LZ_HOUSTON',
      series: 'rt_price',
      model: 'm',
      issued_utc: iso(S0),
      step_minutes: 15,
      points: [
        { ts_utc: iso(S0 + 1800), p10: 1, p50: 2, p90: 3 },
        { ts_utc: iso(S0 + 900), p10: 4, p50: 5, p90: 6 },
      ],
    };
    const { data, actions, hasForecast } = buildPriceModel(history, forecast, 'LZ_HOUSTON');
    const [xs, price, p90, p10] = data;
    expect(hasForecast).toBe(true);
    expect(xs).toEqual([S0, S0 + 300, S0 + 600, S0 + 900, S0 + 1800]);
    expect(price).toEqual([100, 101, 102, 103, null]);
    expect(p90).toEqual([null, null, null, 6, 3]);
    expect(p10).toEqual([null, null, null, 4, 1]);
    expect(actions).toHaveLength(5);
    expect(actions[4]).toBeNull();
  });

  it('copes with an area that has no price', () => {
    const { data } = buildPriceModel([tick(0)], undefined, 'LZ_NORTH');
    expect(data[1]).toEqual([null]);
  });
});

describe('actionSpans', () => {
  it('joins consecutive ticks and splits on holds and gaps', () => {
    const xs = [S0, S0 + 300, S0 + 600, S0 + 900, S0 + 1500];
    const actions = ['charge', 'charge', 'hold', 'sell', 'sell'] as const;
    expect(actionSpans(xs, [...actions])).toEqual([
      { start: S0, end: S0 + 600, action: 'charge' },
      { start: S0 + 900, end: S0 + 1200, action: 'sell' },
      { start: S0 + 1500, end: S0 + 1800, action: 'sell' },
    ]);
  });
});

describe('buildPowerModel', () => {
  it('uses the fleet total in MW, or sums the areas', () => {
    const { data } = buildPowerModel([tick(0, {}, { target: 48_000, actual: -2_000 }), tick(1)]);
    expect(data[1]).toEqual([48, 1.5]);
    expect(data[2]).toEqual([-2, 1.4]);
  });
});

describe('crashMarkers', () => {
  it('marks worker_dead events at their market time', () => {
    const e = (kind: string, detail?: Record<string, unknown>): LiveEvent => ({
      id: kind,
      wall_utc: iso(S0),
      sim_utc: iso(S0 + 600),
      kind,
      severity: 'warn',
      message: '',
      detail,
    });
    expect(crashMarkers([e('worker_dead', { worker_id: 7 }), e('worker_suspect'), e('worker_dead')])).toEqual([
      { x: S0 + 600, label: 'Controller 7' },
      { x: S0 + 600, label: 'Crash' },
    ]);
  });
});
