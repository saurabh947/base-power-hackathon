import { describe, expect, it } from 'vitest';
import type { Results, StrategyResult } from '../../api/types.ts';
import { cityRows, comparisonRows, headlineOf, strategyLabel } from './model.ts';

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
