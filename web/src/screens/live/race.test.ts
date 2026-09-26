import { describe, expect, it } from 'vitest';
import type { Race, Tick, Totals } from '../../api/types.ts';
import { eventClass, eventIcon, eventSpoken } from './eventLook.ts';
import { raceName, raceView, type RaceInput } from './race.ts';

const MINUS = '−';

function race(overrides: Partial<Race> = {}): Race {
  return {
    strategy: 'bands',
    label: 'Simple rule',
    revenue_usd: 31_020.3,
    by_location: {
      LZ_HOUSTON: { revenue_usd: 5_321.4, charge_level: 0.21, action: 'hold' },
      LZ_NORTH: { revenue_usd: 12_000, charge_level: 0.4, action: 'sell' },
    },
    charge_level: 0.43,
    live_minus_race_usd: 15_290.1,
    ...overrides,
  };
}

function totals(revenue = 46_310.4): Totals {
  return {
    revenue_usd: revenue,
    sold_kwh: 0,
    bought_kwh: 0,
    ticks: 10,
    by_location: {
      LZ_HOUSTON: { revenue_usd: 24_608.96, sold_kwh: 0, bought_kwh: 0 },
      LZ_NORTH: { revenue_usd: 11_000, sold_kwh: 0, bought_kwh: 0 },
    },
  };
}

const tick = {
  ts_utc: '2025-07-12T02:00:00Z',
  charge_level: { LZ_HOUSTON: 0.54, LZ_NORTH: 0.6, fleet: 0.57 },
  action: { LZ_HOUSTON: 'sell', LZ_NORTH: 'sell' },
} as unknown as Tick;

const input = (overrides: Partial<RaceInput> = {}): RaceInput => ({
  race: race(),
  liveStrategy: 'forecast',
  totals: totals(),
  tick,
  scope: null,
  ...overrides,
});

describe('the race, all of Texas', () => {
  it('reads like the approved line, with the labels from the data', () => {
    const v = raceView(input());
    expect(v?.summary).toBe('AI forecast $46,310 · Simple rule $31,020 · AI forecast ahead by $15,290');
    expect(v).toMatchObject({ leader: 'live', lead: 'AI forecast ahead by $15,290' });
    expect(v?.live).toMatchObject({ label: 'AI forecast', text: '$46,310', share: 1, action: null });
    expect(v?.race.share).toBeCloseTo(31_020.3 / 46_310.4, 9);
  });

  it('uses live_minus_race_usd for the lead, and says who leads when it is the other side', () => {
    expect(raceView(input({ race: race({ live_minus_race_usd: -1_204.4 }) }))).toMatchObject({
      leader: 'race',
      lead: 'Simple rule ahead by $1,204',
    });
    expect(raceView(input({ race: race({ live_minus_race_usd: 0.3 }) }))).toMatchObject({ leader: 'level', lead: 'Level so far' });
    // Without it, the difference of the two sums.
    expect(raceView(input({ race: race({ live_minus_race_usd: Number.NaN }) }))?.diff).toBeCloseTo(15_290.1, 6);
  });

  it('shows cents while both sums are small, and a loss with a real minus sign', () => {
    const v = raceView(input({ totals: totals(12.345), race: race({ revenue_usd: -3.2, live_minus_race_usd: 15.545 }) }));
    expect(v?.summary).toBe(`AI forecast $12.35 · Simple rule ${MINUS}$3.20 · AI forecast ahead by $15.55`);
    expect(v?.race.share).toBe(0);
  });

  it('gives the charge left on both sides', () => {
    expect(raceView(input())?.charge).toBe('Charge left: AI forecast 57%, simple rule 43%');
    expect(raceView(input({ tick: null }))?.charge).toBeNull();
  });

  it('names the sides the other way round when the live run is the simple rule', () => {
    const v = raceView(
      input({
        liveStrategy: 'bands',
        race: race({ strategy: 'forecast', label: 'AI forecast (TimesFM-3)', revenue_usd: 46_310.4, live_minus_race_usd: -15_290.1 }),
        totals: totals(31_020.3),
      }),
    );
    expect(v?.summary).toBe('Simple rule $31,020 · AI forecast $46,310 · AI forecast ahead by $15,290');
    expect(v?.leader).toBe('race');
  });

  it('works out the live method when the settings are not known yet', () => {
    expect(raceView(input({ liveStrategy: null }))?.live.label).toBe('AI forecast');
    expect(raceView(input({ liveStrategy: null, race: race({ strategy: 'forecast', label: '' }) }))?.live.label).toBe('Simple rule');
  });

  it('is hidden while idle', () => {
    expect(raceView(input({ race: null }))).toBeNull();
    expect(raceView(input({ race: { ...race(), revenue_usd: Number.NaN } }))).toBeNull();
  });
});

describe('the race, one city', () => {
  it('shows the city’s numbers from race.by_location, with what each side is doing', () => {
    const v = raceView(input({ scope: 'LZ_HOUSTON' }));
    expect(v?.summary).toBe('AI forecast $24,609 · Simple rule $5,321 · AI forecast ahead by $19,288');
    expect(v?.live).toMatchObject({ action: 'sell', charge: 0.54 });
    expect(v?.race).toMatchObject({ action: 'hold', charge: 0.21 });
    expect(v?.charge).toBe('Charge left: AI forecast 54%, simple rule 21%');
  });

  it('can be behind in one city while ahead overall', () => {
    expect(raceView(input({ scope: 'LZ_NORTH' }))).toMatchObject({ leader: 'race', lead: 'Simple rule ahead by $1,000' });
  });

  it('is hidden for a city without race numbers', () => {
    expect(raceView(input({ scope: 'LZ_AEN' }))).toBeNull();
  });

  it('names the other side in a sentence', () => {
    expect(raceName(race())).toBe('the simple rule');
    expect(raceName({ strategy: 'forecast', label: 'AI forecast (TimesFM-3)' })).toBe('the AI forecast');
    expect(raceName({ strategy: 'peak', label: '' })).toBe('the evening peak');
  });
});

describe('the third bar: all batteries online (projected)', () => {
  const potential = {
    label: 'AI forecast, all batteries online',
    revenue_usd: 47_100.9,
    by_location: { LZ_HOUSTON: { revenue_usd: 25_400 } },
    shortfall_usd: 790.5,
  };

  it('stays hidden until race.shows_potential', () => {
    expect(raceView(input({ race: race({ potential, shows_potential: false }) }))).toMatchObject({ potential: null, shortfall: null });
    expect(raceView(input({ race: race({ potential }) }))?.potential).toBeNull(); // an older backend: no flag
  });

  it('shows the projection beside the two fleets, with what the outages cost', () => {
    const v = raceView(input({ race: race({ potential, shows_potential: true }) }));
    expect(v?.potential).toMatchObject({ label: 'All batteries online', text: '$47,101', share: 1 });
    expect(v?.live.share).toBeCloseTo(46_310.4 / 47_100.9, 9);
    expect(v?.shortfall).toEqual({ usd: 790.5, text: 'Outages cost $791 so far' });
    expect(v?.summary).toBe(
      'AI forecast $46,310 · Simple rule $31,020 · All batteries online (projected) $47,101 · AI forecast ahead by $15,290 · Outages cost $791 so far',
    );
  });

  it('per city, from potential.by_location, the shortfall worked out against the city’s own money', () => {
    const v = raceView(input({ scope: 'LZ_HOUSTON', race: race({ potential, shows_potential: true }) }));
    expect(v?.potential?.text).toBe('$25,400');
    expect(v?.shortfall?.usd).toBeCloseTo(25_400 - 24_608.96, 9);
    expect(v?.shortfall?.text).toBe('Outages cost $791 so far');
    // A city the potential has no numbers for: no third bar there.
    expect(raceView(input({ scope: 'LZ_NORTH', race: race({ potential, shows_potential: true }) }))?.potential).toBeNull();
  });

  it('says so plainly when the outages have cost nothing (yet)', () => {
    const v = raceView(input({ race: race({ potential: { ...potential, shortfall_usd: -3 }, shows_potential: true }) }));
    expect(v?.shortfall?.text).toBe('Outages have cost nothing so far');
  });
});

describe('race updates in "What just happened"', () => {
  it('get a flag of their own and say so to screen readers, coloured by severity like the rest', () => {
    expect(eventIcon({ kind: 'race_update', severity: 'good' })).toBe('flag');
    expect(eventSpoken({ kind: 'race_update', severity: 'good' })).toBe('Race update: ');
    expect(eventClass({ kind: 'race_update', severity: 'info' })).toBe('feed-info feed-race');
  });

  it('mark the moment the AI’s prediction takes over with its sparkle, in the prediction’s violet', () => {
    expect(eventIcon({ kind: 'prediction_started', severity: 'info' })).toBe('ai');
    expect(eventClass({ kind: 'prediction_started', severity: 'info' })).toBe('feed-predict');
    expect(eventSpoken({ kind: 'prediction_started', severity: 'info' })).toBe('Prediction: ');
  });

  it('leave the other events as they were', () => {
    expect(eventIcon({ kind: 'worker_dead', severity: 'warn' })).toBe('alert');
    expect(eventIcon({ kind: 'run_finished', severity: 'good' })).toBe('check');
    expect(eventIcon({ kind: 'price_spike', severity: 'info' })).toBe('info');
    expect(eventIcon({ kind: 'x', severity: 'odd' as never })).toBe('info');
    expect(eventSpoken({ kind: 'worker_dead', severity: 'warn' })).toBe('Warning: ');
    expect(eventSpoken({ kind: 'price_spike', severity: 'info' })).toBe('');
    expect(eventClass({ kind: 'x', severity: 'odd' as never })).toBe('feed-info');
  });
});
