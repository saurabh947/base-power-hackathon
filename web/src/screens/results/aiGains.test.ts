import { describe, expect, it } from 'vitest';
import type { AiGains, StrategyGains } from '../../api/types.ts';
import { shortLabel } from '../../lib/format.ts';
import {
  fleetSizeOf,
  fmtMwh,
  fmtSignedPct,
  fmtSignedUsd,
  fmtTimes,
  gainsView,
  pickYear,
  readAiGains,
  spikeCard,
  upliftOf,
  yearOptions,
  yearsNote,
} from './aiGains.ts';

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
    const view = gainsView(read(measured()), null, FLEET);
    expect(view.year.year).toBe(2025);
    expect(view.headline?.sentence).toBe(
      '+$67 per battery per year (+54%) vs the simple rule, $1.35 million more a year for the 20,000-battery fleet',
    );
    expect(view.headline).toMatchObject({ amount: '+$67', pctText: '+54%', versus: 'vs the simple rule' });
  });

  it('follows the numbers it is given (nothing hardcoded)', () => {
    const g = measured();
    g.years[0].uplift = { per_battery_usd_year: 80.5, pct: 0.61, fleet_usd_year: 1_610_000 };
    expect(gainsView(read(g), 2025, FLEET).headline?.sentence).toBe(
      '+$81 per battery per year (+61%) vs the simple rule, $1.61 million more a year for the 20,000-battery fleet',
    );
    expect(gainsView(read(measured()), 2025, 10_000).headline?.fleetText).toBe(
      '$1.35 million more a year for the 10,000-battery fleet',
    );
  });

  it('fills a missing uplift from the two methods and the fleet size', () => {
    const g = measured();
    g.years[0].uplift = {} as never;
    const y = read(g).years[0];
    const up = upliftOf(y, 'forecast', 'bands', FLEET);
    expect(up?.perBattery).toBeCloseTo(190.95 - 123.67, 9);
    expect(up?.pct).toBeCloseTo((190.95 - 123.67) / 123.67, 9);
    expect(up?.fleet).toBeCloseTo((190.95 - 123.67) * FLEET, 6);
    expect(gainsView(read(g), 2025, FLEET).headline?.sentence).toBe(
      '+$67 per battery per year (+54%) vs the simple rule, $1.35 million more a year for the 20,000-battery fleet',
    );
    // No fleet size and no fleet sums: the fleet part is left out rather than guessed.
    expect(upliftOf(y, 'forecast', 'bands', null)?.fleet).toBeNull();
  });

  it('knows the fleet size from the results, or from the AI’s own sums', () => {
    const y = read(measured()).years[0];
    expect(fleetSizeOf(y, 'forecast', 20_000)).toBe(20_000);
    expect(fleetSizeOf(y, 'forecast', null)).toBe(20_000); // 3,819,000 / 190.95
    expect(fleetSizeOf(y, 'forecast', Number.NaN)).toBe(20_000);
  });

  it('says so honestly when the AI earns less', () => {
    const g = measured();
    g.years[0].uplift = { per_battery_usd_year: -12.4, pct: -0.1, fleet_usd_year: -248_000 };
    expect(gainsView(read(g), 2025, FLEET).headline?.sentence).toBe(
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

  it('writes energy in MWh and shortens method names', () => {
    expect(fmtMwh(7011.3)).toBe('7,011 MWh');
    expect(fmtMwh(610.9)).toBe('611 MWh');
    expect(fmtMwh(2.46)).toBe('2.5 MWh');
    expect(shortLabel('AI forecast (TimesFM-3)')).toBe('AI forecast');
    expect(shortLabel('Simple rule')).toBe('Simple rule');
  });
});

describe('the cards', () => {
  const view = gainsView(read(measured()), 2025, FLEET);

  it('(a) earns more: per battery and the fleet difference', () => {
    expect(view.earns).toMatchObject({
      aiText: '$191',
      baseText: '$124',
      fleetText: '$3.82 million vs $2.47 million a year',
      fleetDiffText: '+$1.35 million a year',
      aiShare: 1,
    });
    expect(view.earns?.baseShare).toBeCloseTo(123.67 / 190.95, 9);
    expect(view.earns?.ai.short).toBe('AI forecast');
  });

  it('(b) closer to perfect: simple rule, evening peak, then the AI, as shares of the hindsight best', () => {
    expect(view.perfect?.bars.map((b) => [b.key, b.label, b.text, b.ai])).toEqual([
      ['bands', 'Simple rule', '29%', false],
      ['peak', 'Evening peak', '32%', false],
      ['forecast', 'AI forecast', '45%', true],
    ]);
    expect(view.perfect?.bars[2].share).toBeCloseTo(0.453, 9);
    expect(view.perfect?.bestText).toBe('$421');
  });

  it('(b) works out a missing share from the hindsight best', () => {
    const g = measured();
    g.years[0].strategies.bands.share_of_best = undefined as never;
    const bars = gainsView(read(g), 2025, FLEET).perfect?.bars ?? [];
    expect(bars[0].share).toBeCloseTo(123.67 / 421.29, 9);
    expect(bars[0].text).toBe('29%');
  });

  it('(c) soaks up surplus power: MWh bought at $0 or less, and how many times as much', () => {
    expect(view.surplus).toMatchObject({ aiText: '7,011 MWh', baseText: '1,239 MWh', times: '5.7×', aiShare: 1 });
    expect(view.surplus?.homes).toBe('about what 190,000 homes use in a day');
  });

  it('(d) ready for spikes: the 2024 numbers', () => {
    expect(view.spike).toMatchObject({
      year: 2024,
      aiChargeText: '52%',
      baseChargeText: '24%',
      aiMwhText: '611 MWh',
      baseMwhText: '76 MWh',
      times: '8×',
      eventsText: '2024 had 53 price spikes of $1,000/MWh or more ($1.00 per kWh).',
    });
  });
});

describe('the year switch', () => {
  const g = read(measured());

  it('offers the pitch year first, labelled for a first-time visitor', () => {
    // A no-break space inside the note, so a narrow switch wraps after the year.
    expect(yearOptions(g)).toEqual([
      { year: 2025, label: '2025 (headline year)', isPitch: true },
      { year: 2024, label: '2024 (check year)', isPitch: false },
    ]);
    expect(yearsNote(g)).toBe('The headline number above is for 2025. 2024 is a second full year, to check that the gain holds.');
  });

  it('drives the headline and cards a–c', () => {
    const v = gainsView(g, 2024, FLEET);
    expect(v.year.year).toBe(2024);
    expect(v.headline?.sentence).toBe(
      '+$158 per battery per year (+139%) vs the simple rule, $3.15 million more a year for the 20,000-battery fleet',
    );
    expect(v.earns).toMatchObject({ aiText: '$271', baseText: '$114', fleetText: '$5.43 million vs $2.27 million a year' });
    expect(v.perfect?.bars.map((b) => b.text)).toEqual(['22%', '29%', '52%']);
    expect(v.surplus).toMatchObject({ aiText: '6,361 MWh', baseText: '905 MWh', times: '7×' });
  });

  it('keeps the spike card on 2024 whichever year is picked', () => {
    expect(gainsView(g, 2025, FLEET).spike).toEqual(gainsView(g, 2024, FLEET).spike);
    expect(gainsView(g, 2025, FLEET).spike?.year).toBe(2024);
  });

  it('falls back to the pitch year for a year that is not there', () => {
    expect(pickYear(g, 2019).year).toBe(2025);
    expect(pickYear(g, null).year).toBe(2025);
  });

  it('puts the pitch year first even when the server does not', () => {
    const raw = measured();
    raw.years.reverse();
    expect(read(raw).years.map((y) => y.year)).toEqual([2025, 2024]);
  });
});

describe('the spike card is 2024 only', () => {
  it('is hidden when there is no 2024', () => {
    const raw = measured();
    raw.years = raw.years.filter((y) => y.year !== 2024);
    const g = read(raw);
    expect(spikeCard(g)).toBeNull();
    expect(gainsView(g, 2025, FLEET).spike).toBeNull();
    expect(yearOptions(g).map((o) => o.year)).toEqual([2025]);
    expect(yearsNote(g)).toBeNull();
  });

  it('is shown for 2024 even when 2024 is the only year', () => {
    const raw = measured();
    raw.years = raw.years.filter((y) => y.year === 2024);
    expect(spikeCard(read(raw))?.aiChargeText).toBe('52%');
  });

  it('is hidden when the spike numbers are missing, and says "1 price spike" for one', () => {
    const missing = measured();
    missing.years[1].strategies.forecast.spike_mwh = null as never;
    expect(spikeCard(read(missing))).toBeNull();
    const one = measured();
    one.years[1].strategies.forecast.spike_events = 1;
    expect(spikeCard(read(one))?.eventsText).toBe('2024 had 1 price spike of $1,000/MWh or more ($1.00 per kWh).');
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

  it('keeps the first of a repeated year and fills a missing label', () => {
    const raw = measured();
    raw.years.push({ ...raw.years[0], uplift: { per_battery_usd_year: 1, pct: 0.01, fleet_usd_year: 20_000 } });
    raw.years[0].strategies.bands.label = '';
    const g = read(raw);
    expect(g.years.map((y) => y.year)).toEqual([2025, 2024]);
    expect(g.years[0].uplift.per_battery_usd_year).toBe(67.28);
    expect(g.years[0].strategies.bands.label).toBe('Simple rule');
  });

  it('defaults the AI and the baseline to the documented ids', () => {
    const raw = measured() as unknown as Record<string, unknown>;
    delete raw.ai;
    delete raw.baseline;
    expect(read(raw)).toMatchObject({ ai: 'forecast', baseline: 'bands' });
  });
});
