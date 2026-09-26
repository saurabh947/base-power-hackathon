import { describe, expect, it } from 'vitest';
import type { Results, StrategyResult } from '../../api/types.ts';
import { cityRows, comparisonRows, headlineOf, isProjectedDay, roundedParts, splitOf, strategyLabel, yearSplit } from './model.ts';

function strategy(key: string, perBattery: number, headline = false, label = ''): StrategyResult {
  return {
    strategy: key,
    label,
    run_id: `run-${key}`,
    is_headline: headline,
    per_battery_usd_year: perBattery,
    fleet_usd_year: perBattery * 20_000,
    usd_per_kwh_year: 0,
    usd_per_kw_year: 0,
    cycles_per_year: 0,
    per_location_usd: {},
    top_days: [],
  };
}

function results(strategies: StrategyResult[], ceiling: number | null = 251): Results {
  return {
    year: 2025,
    generated_utc: '2026-09-26T00:00:00Z',
    fleet_size: 20_000,
    fleet_power_mw: 165,
    fleet_energy_mwh: 300,
    assumptions: [],
    strategies,
    ceiling: ceiling === null ? null : { label: '', per_battery_usd_year: ceiling, fleet_usd_year: ceiling * 20_000 },
  };
}

describe('comparisonRows', () => {
  const r = results([strategy('peak', 128.5), strategy('forecast', 161.8, true, 'AI forecast (TimesFM-3)'), strategy('bands', 104.2)]);
  const rows = comparisonRows(r, headlineOf(r));

  it('orders simple rule, AI forecast, evening peak, then the hindsight ceiling', () => {
    expect(rows.map((x) => x.key)).toEqual(['bands', 'forecast', 'peak', 'ceiling']);
    expect(rows.map((x) => x.label)).toEqual(['Simple rule', 'AI forecast (TimesFM-3)', 'Evening peak', 'Best possible (perfect hindsight)']);
  });

  it('marks the headline method (green) and the ceiling (hatched)', () => {
    expect(rows.filter((x) => x.headline).map((x) => x.key)).toEqual(['forecast']);
    expect(rows.filter((x) => x.ceiling).map((x) => x.key)).toEqual(['ceiling']);
  });

  it('sizes the bars against the longest one', () => {
    expect(rows.at(-1)?.share).toBe(1);
    expect(rows[1].share).toBeCloseTo(161.8 / 251, 9);
    for (const row of rows) {
      expect(row.share).toBeGreaterThanOrEqual(0);
      expect(row.share).toBeLessThanOrEqual(1);
    }
  });

  it('copes with no ceiling, a loss and a missing number', () => {
    const odd = results([strategy('bands', -5), strategy('forecast', Number.NaN, true), strategy('peak', 40)], null);
    const oddRows = comparisonRows(odd, headlineOf(odd));
    expect(oddRows.map((x) => x.key)).toEqual(['bands', 'forecast', 'peak']);
    expect(oddRows.map((x) => x.share)).toEqual([0, 0, 1]);
  });

  it('falls back to the first method when none is marked as the headline', () => {
    const none = results([strategy('bands', 1), strategy('forecast', 2)]);
    expect(headlineOf(none)?.strategy).toBe('bands');
    expect(comparisonRows(none, headlineOf(none)).find((x) => x.headline)?.key).toBe('bands');
    expect(headlineOf(null)).toBeNull();
    expect(strategyLabel(strategy('forecast', 1))).toMatch(/AI forecast/);
  });
});

const split = (s: StrategyResult, real: number, projected: number): StrategyResult => ({
  ...s,
  real_per_battery_usd: real,
  projected_per_battery_usd: projected,
});

describe('2026: real prices, then the AI prediction (real_until_utc)', () => {
  const y2026: Results = {
    ...results([split(strategy('bands', 130), 95, 35), split(strategy('forecast', 200, true), 140, 60), split(strategy('peak', 150), 110, 40)]),
    year: 2026,
    real_until_utc: '2026-09-21T05:00:00Z',
    ceiling: { label: '', per_battery_usd_year: 400, fleet_usd_year: 8e6, real_per_battery_usd: 290, projected_per_battery_usd: 110 },
  };

  it('splits the year where the real prices end: Jan 1 – Sep 20 real, Sep 21 – Dec 31 projected', () => {
    expect(yearSplit(y2026)).toEqual({
      lastRealDay: '2026-09-20',
      firstProjectedDay: '2026-09-21',
      realSpan: 'Jan 1 – Sep 20',
      projectedSpan: 'Sep 21 – Dec 31',
    });
    expect(yearSplit({ year: 2025, real_until_utc: null })).toBeNull();
    expect(yearSplit({ year: 2025 })).toBeNull();
    // A prediction outside the year does not split it.
    expect(yearSplit({ year: 2025, real_until_utc: '2026-09-21T05:00:00Z' })).toBeNull();
  });

  it('rounds the parts so they add up to the rounded total', () => {
    expect(roundedParts(151.6, 53.8)).toEqual([151, 54, 205]); // not 152 + 54
    expect(roundedParts(97.9, 33.3)).toEqual([98, 33, 131]);
    expect(roundedParts(10.5, 10.5)).toEqual([10, 11, 21]);
    expect(roundedParts(45.3, 20.4)).toEqual([45, 21, 66]);
    expect(roundedParts(-2.6, 5.4)).toEqual([-3, 6, 3]);
    for (const [a, b] of [[1.49, 1.49], [0.5, 0.5], [100.25, -0.75], [7.7, 7.7]]) {
      const [ra, rb, t] = roundedParts(a, b);
      expect(ra + rb).toBe(t);
      expect(Math.abs(ra - a)).toBeLessThan(1);
      expect(Math.abs(rb - b)).toBeLessThan(1);
    }
  });

  it('marks the days after the real prices as projected', () => {
    const s = yearSplit(y2026);
    expect(isProjectedDay('2026-09-20', s)).toBe(false);
    expect(isProjectedDay('2026-09-21', s)).toBe(true);
    expect(isProjectedDay('2026-12-31', s)).toBe(true);
    expect(isProjectedDay('2026-12-31', null)).toBe(false);
  });

  it('draws each bar as its real part and its projected part, the ceiling too', () => {
    const rows = comparisonRows(y2026, headlineOf(y2026));
    const ai = rows.find((x) => x.key === 'forecast');
    expect(ai?.split).toMatchObject({ real: 140, projected: 60, total: 200 });
    expect(ai?.split?.realShare).toBeCloseTo(140 / 400, 9);
    expect((ai?.split?.realShare ?? 0) + (ai?.split?.projectedShare ?? 0)).toBeCloseTo(ai?.share ?? 0, 9);
    expect(rows.find((x) => x.ceiling)?.split).toMatchObject({ real: 290, projected: 110 });
    // A fully real year (or a backend that does not split) draws plain bars.
    expect(comparisonRows(results([strategy('forecast', 200, true)]), null).every((x) => x.split === null)).toBe(true);
    expect(splitOf(strategy('bands', 1))).toBeNull();
  });
});

describe('cityRows', () => {
  it('lists the four Texas Triangle cities biggest first, with bars against the biggest', () => {
    const rows = cityRows({ LZ_HOUSTON: 1_177_904, LZ_NORTH: 1_242_624, LZ_CPS: 429_094, LZ_AEN: 384_437 });
    expect(rows.map((r) => r.name)).toEqual(['Dallas–Fort Worth', 'Houston', 'San Antonio', 'Austin']);
    expect(rows[0].share).toBe(1);
    expect(rows[3].share).toBeCloseTo(384_437 / 1_242_624, 9);
  });

  it('works for an older two-city result, unknown locations and empty data', () => {
    expect(cityRows({ LZ_HOUSTON: 2, LZ_NORTH: 1 }).map((r) => r.location)).toEqual(['LZ_HOUSTON', 'LZ_NORTH']);
    expect(cityRows({ LZ_WEST: 5 })).toEqual([{ location: 'LZ_WEST', name: 'LZ_WEST', usd: 5, share: 1 }]);
    expect(cityRows({})).toEqual([]);
    expect(cityRows(undefined)).toEqual([]);
  });

  it('leaves out missing numbers and never draws a negative bar', () => {
    const rows = cityRows({ LZ_HOUSTON: 10, LZ_NORTH: Number.NaN, LZ_CPS: -3 } as Record<string, number>);
    expect(rows.map((r) => r.location)).toEqual(['LZ_HOUSTON', 'LZ_CPS']);
    expect(rows[1].share).toBe(0);
  });
});
