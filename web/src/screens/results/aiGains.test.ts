import { describe, expect, it } from 'vitest';
import type { AiGains, StrategyGains } from '../../api/types.ts';
import { fleetSizeOf, fmtMwh, fmtSignedPct, fmtSignedUsd, fmtTimes, gainsView, readAiGains, upliftOf } from './aiGains.ts';

const MINUS = '−';
const FLEET = 20_000;

function gains(
  label: string,
  perBattery: number,
  best: number,
  o: { surplus: number; spike: number; charge: number; events: number },
): StrategyGains {
  return {
    label,
    per_battery_usd_year: perBattery,
    fleet_usd_year: Math.round(perBattery * FLEET),
    share_of_best: Math.round((perBattery / best) * 1000) / 1000,
    surplus_mwh: o.surplus,
    spike_mwh: o.spike,
    spike_events: o.events,
    spike_charge_level: o.charge,
    sold_mwh: 0,
    bought_mwh: 0,
    avg_sell_usd_mwh: 0,
    avg_buy_usd_mwh: 0,
  };
}

/** The measured numbers (RUNLOG.md, "What the AI adds, measured"; the 4-city full-year results). */
function measured(): AiGains {
  return {
    ai: 'forecast',
    baseline: 'bands',
    years: [
      {
        year: 2025,
        is_pitch: true,
        best_possible_per_battery_usd_year: 421.29,
        strategies: {
          forecast: gains('AI forecast (TimesFM-3)', 190.95, 421.29, { surplus: 7011.3, spike: 25.2, charge: 0.231, events: 21 }),
          bands: gains('Simple rule', 123.67, 421.29, { surplus: 1239, spike: 27.9, charge: 0.24, events: 21 }),
          peak: gains('Evening peak', 136.7, 421.29, { surplus: 5365, spike: 36.9, charge: 0.35, events: 21 }),
        },
        uplift: { per_battery_usd_year: 67.28, pct: 0.544, fleet_usd_year: 1_345_600 },
      },
      {
        year: 2024,
        is_pitch: false,
        best_possible_per_battery_usd_year: 526.54,
        strategies: {
          forecast: gains('AI forecast (TimesFM-3)', 271.45, 526.54, { surplus: 6361, spike: 610.9, charge: 0.52, events: 53 }),
          bands: gains('Simple rule', 113.71, 526.54, { surplus: 905, spike: 76.3, charge: 0.24, events: 53 }),
          peak: gains('Evening peak', 150.51, 526.54, { surplus: 3552, spike: 298.4, charge: 0.64, events: 53 }),
        },
        uplift: { per_battery_usd_year: 157.74, pct: 1.387, fleet_usd_year: 3_154_800 },
      },
    ],
  };
}

const read = (raw: unknown) => {
  const g = readAiGains(raw);
  if (!g) throw new Error('expected usable gains');
  return g;
};

describe('the headline', () => {
  it('reads like the approved line, from uplift and the fleet size', () => {
    const view = gainsView(read(measured()), FLEET);
    expect(view.year.year).toBe(2025);
    expect(view.headline?.sentence).toBe(
      '+$67 per battery per year (+54%) vs the simple rule, $1.35 million more a year for the 20,000-battery fleet',
    );
    expect(view.headline).toMatchObject({ amount: '+$67', pctText: '+54%', versus: 'vs the simple rule' });
  });

  it('follows the numbers it is given (nothing hardcoded)', () => {
    const g = measured();
    g.years[0].uplift = { per_battery_usd_year: 80.5, pct: 0.61, fleet_usd_year: 1_610_000 };
    expect(gainsView(read(g), FLEET).headline?.sentence).toBe(
      '+$81 per battery per year (+61%) vs the simple rule, $1.61 million more a year for the 20,000-battery fleet',
    );
    expect(gainsView(read(measured()), 10_000).headline?.fleetText).toBe(
      '$1.35 million more a year for the 10,000-battery fleet',
    );
  });

  it('fills a missing uplift from the two methods and the fleet size', () => {
    const g = measured();
    g.years[0].uplift = {} as never;
    const y = read(g).year;
    const up = upliftOf(y, 'forecast', 'bands', FLEET);
    expect(up?.perBattery).toBeCloseTo(190.95 - 123.67, 9);
    expect(up?.pct).toBeCloseTo((190.95 - 123.67) / 123.67, 9);
    expect(up?.fleet).toBeCloseTo((190.95 - 123.67) * FLEET, 6);
    expect(gainsView(read(g), FLEET).headline?.sentence).toBe(
      '+$67 per battery per year (+54%) vs the simple rule, $1.35 million more a year for the 20,000-battery fleet',
    );
    // No fleet size and no fleet sums: the fleet part is left out rather than guessed.
    expect(upliftOf(y, 'forecast', 'bands', null)?.fleet).toBeNull();
  });

  it('knows the fleet size from the results, or from the AI’s own sums', () => {
    const y = read(measured()).year;
    expect(fleetSizeOf(y, 'forecast', 20_000)).toBe(20_000);
    expect(fleetSizeOf(y, 'forecast', null)).toBe(20_000); // 3,819,000 / 190.95
    expect(fleetSizeOf(y, 'forecast', Number.NaN)).toBe(20_000);
  });

  it('says so honestly when the AI earns less', () => {
    const g = measured();
    g.years[0].uplift = { per_battery_usd_year: -12.4, pct: -0.1, fleet_usd_year: -248_000 };
    expect(gainsView(read(g), FLEET).headline?.sentence).toBe(
      `${MINUS}$12 per battery per year (${MINUS}10%) vs the simple rule, $248,000 less a year for the 20,000-battery fleet`,
    );
  });
});

describe('formatting', () => {
  it('signs money and percentages', () => {
    expect(fmtSignedUsd(67.28)).toBe('+$67');
    expect(fmtSignedUsd(157.74)).toBe('+$158');
    expect(fmtSignedUsd(0.4)).toBe('+$0.40');
    expect(fmtSignedUsd(-12.4)).toBe(`${MINUS}$12`);
    expect(fmtSignedUsd(Number.NaN)).toBe('–');
    expect(fmtSignedPct(0.544)).toBe('+54%');
    expect(fmtSignedPct(1.387)).toBe('+139%');
    expect(fmtSignedPct(-0.08)).toBe(`${MINUS}8%`);
  });

  it('says how many times as much, without a trailing .0', () => {
    expect(fmtTimes(7011.3, 1239)).toBe('5.7×');
    expect(fmtTimes(610.9, 76.3)).toBe('8×');
    expect(fmtTimes(6361, 905)).toBe('7×');
    expect(fmtTimes(1230, 100)).toBe('12×');
    expect(fmtTimes(5, 0)).toBeNull();
    expect(fmtTimes(0, 5)).toBeNull();
    expect(fmtTimes(Number.NaN, 5)).toBeNull();
  });

  it('writes energy in MWh', () => {
    expect(fmtMwh(7011.3)).toBe('7,011 MWh');
    expect(fmtMwh(610.9)).toBe('611 MWh');
    expect(fmtMwh(2.46)).toBe('2.5 MWh');
    expect(fmtMwh(Number.NaN)).toBe('–');
  });
});

describe('surplus power', () => {
  const view = gainsView(read(measured()), FLEET);

  it('soaks up surplus power: MWh bought at $0 or less, and how many times as much', () => {
    expect(view.surplus).toMatchObject({ aiText: '7,011 MWh', baseText: '1,239 MWh', times: '5.7×', aiShare: 1 });
    expect(view.surplus?.homes).toBe('about what 190,000 homes use in a day');
  });

});

describe('the pitch year only (no year switch, no spike card)', () => {
  it('shows the first year flagged is_pitch and ignores the others', () => {
    const g = read(measured());
    expect(g.year.year).toBe(2025);
    expect(Object.keys(g)).toEqual(['ai', 'baseline', 'year']);
    const view = gainsView(g, FLEET);
    expect(Object.keys(view)).toEqual(['year', 'headline', 'surplus', 'split']);
    expect(view.split).toBeNull(); // 2025 is real prices all year
    // 2024's much bigger gain never shows, whatever order the server sends.
    const reversed = measured();
    reversed.years.reverse();
    expect(read(reversed).year.year).toBe(2025);
    expect(gainsView(read(reversed), FLEET).headline?.amount).toBe('+$67');
  });

  it('hides the section when the pitch year cannot be compared, rather than showing another year', () => {
    const raw = measured();
    delete (raw.years[0].strategies as Record<string, unknown>).bands;
    expect(readAiGains(raw)).toBeNull();
  });

  it('uses the first usable year when no year is flagged (older data)', () => {
    const raw = measured();
    for (const y of raw.years) y.is_pitch = false;
    expect(read(raw).year.year).toBe(2025);
  });
});

describe('the year the Results page shows (GET /results/ai-gains?year=)', () => {
  it('reads that year’s entry, flagged as the pitch or not', () => {
    expect(readAiGains(measured(), 2024)?.year.year).toBe(2024);
    expect(readAiGains(measured(), 2025)?.year.year).toBe(2025);
    expect(gainsView(readAiGains(measured(), 2024) as NonNullable<ReturnType<typeof readAiGains>>, FLEET).headline?.amount).toBe('+$158');
  });

  it('never shows another year’s numbers as this one’s (a backend that ignores ?year=)', () => {
    expect(readAiGains(measured(), 2026)).toBeNull();
  });

  it('splits a 2026 uplift into its real and projected parts, from the results', () => {
    const g = read(measured());
    const span = { lastRealDay: '2026-09-20', firstProjectedDay: '2026-09-21', realSpan: 'Jan 1 – Sep 20', projectedSpan: 'Sep 21 – Dec 31' };
    const view = gainsView(g, FLEET, {
      span,
      ai: { real: 140, projected: 60, total: 200 },
      base: { real: 95, projected: 35, total: 130 },
    });
    expect(view.split).toEqual({
      real: 'Real Jan 1 – Sep 20: +$45 per battery',
      projected: 'Projected Sep 21 – Dec 31: +$25',
      estimate: '2025 estimate: +$70',
    });
  });

  it('keeps every shown dollar adding up, however the parts round', () => {
    const g = read(measured());
    const span = { lastRealDay: '2026-09-20', firstProjectedDay: '2026-09-21', realSpan: 'Jan 1 – Sep 20', projectedSpan: 'Sep 21 – Dec 31' };
    const split = gainsView(g, FLEET, {
      span,
      ai: { real: 151.6, projected: 53.8, total: 205.4 }, // $151 + $54 = $205, not $152 + $54
      base: { real: 97.9, projected: 33.3, total: 131.2 }, // $98 + $33 = $131
    }).split;
    expect([split?.real, split?.projected, split?.estimate]).toEqual([
      'Real Jan 1 – Sep 20: +$53 per battery',
      'Projected Sep 21 – Dec 31: +$21',
      '2025 estimate: +$74',
    ]);
  });
});

describe('readAiGains', () => {
  it('turns anything unusable into null (the section is hidden)', () => {
    expect(readAiGains(null)).toBeNull();
    expect(readAiGains({ error: 'no_results', message: 'No full-year results yet.' })).toBeNull();
    expect(readAiGains({ ai: 'forecast', baseline: 'bands', years: [] })).toBeNull();
    // A year without the baseline has nothing to compare with.
    const raw = measured();
    for (const y of raw.years) delete (y.strategies as Record<string, unknown>).bands;
    expect(readAiGains(raw)).toBeNull();
  });

  it('keeps the first pitch entry of a repeated year and fills a missing label', () => {
    const raw = measured();
    raw.years.push({ ...raw.years[0], uplift: { per_battery_usd_year: 1, pct: 0.01, fleet_usd_year: 20_000 } });
    raw.years[0].strategies.bands.label = '';
    const g = read(raw);
    expect(g.year.uplift.per_battery_usd_year).toBe(67.28);
    expect(g.year.strategies.bands.label).toBe('Simple rule');
  });

  it('defaults the AI and the baseline to the documented ids', () => {
    const raw = measured() as unknown as Record<string, unknown>;
    delete raw.ai;
    delete raw.baseline;
    expect(read(raw)).toMatchObject({ ai: 'forecast', baseline: 'bands' });
  });
});
