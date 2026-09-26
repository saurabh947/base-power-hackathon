import { describe, expect, it } from 'vitest';
import {
  actionLabel,
  areaName,
  areaShortName,
  fmtClock,
  fmtCount,
  fmtDate,
  fmtDuration,
  fmtEnergy,
  fmtHomesDay,
  fmtHomesPower,
  fmtList,
  fmtLocalDate,
  fmtPercent,
  fmtPercentValue,
  fmtPower,
  fmtPowerFlow,
  fmtPresetLabel,
  fmtPrice,
  fmtPriceKwh,
  fmtPriceKwhShort,
  fmtPriceMwh,
  fmtRange,
  fmtSpeed,
  fmtSpeedShort,
  fmtTimeOfDay,
  fmtUsd,
  fmtUsdLarge,
  localDateOf,
  lowerFirst,
  powerDirection,
  roundSignificant,
  shortLabel,
  strategyName,
} from './format.ts';

const MINUS = '−';

describe('prices carry a unit and a per-kWh translation', () => {
  it('matches the UX guide example', () => {
    expect(fmtPrice(120)).toBe('$120/MWh (12¢ per kWh)');
  });

  it('keeps cents for small prices and switches to dollars for spikes', () => {
    expect(fmtPrice(42.1)).toBe('$42.10/MWh (4.2¢ per kWh)');
    expect(fmtPrice(2250.15)).toBe('$2,250/MWh ($2.25 per kWh)');
    expect(fmtPriceMwh(99.99)).toBe('$99.99/MWh');
    expect(fmtPriceKwh(1000)).toBe('$1.00 per kWh');
  });

  it('has a short per-kWh form for tight spaces', () => {
    expect(fmtPriceKwhShort(120)).toBe('12¢/kWh');
    expect(fmtPriceKwhShort(1757.28)).toBe('$1.76/kWh');
    expect(fmtPriceKwhShort(Number.NaN)).toBe('–');
  });

  it('shows negative prices with a real minus sign', () => {
    expect(fmtPrice(-5)).toBe(`${MINUS}$5.00/MWh (${MINUS}0.5¢ per kWh)`);
    expect(fmtPriceKwh(-0.2)).toBe('0.0¢ per kWh');
  });

  it('never prints NaN', () => {
    expect(fmtPrice(Number.NaN)).toBe('–');
    expect(fmtUsd(Number.POSITIVE_INFINITY)).toBe('–');
  });
});

describe('money', () => {
  it('uses cents below $1,000 and whole dollars above', () => {
    expect(fmtUsd(12.3)).toBe('$12.30');
    expect(fmtUsd(1234.5)).toBe('$1,235');
    expect(fmtUsd(1234.5, 2)).toBe('$1,234.50');
    expect(fmtUsd(-1.61)).toBe(`${MINUS}$1.61`);
  });

  it('says "million" for fleet-sized sums', () => {
    expect(fmtUsdLarge(3_236_000)).toBe('$3.2 million');
    expect(fmtUsdLarge(1_250_000_000)).toBe('$1.25 billion');
    expect(fmtUsdLarge(412_600)).toBe('$412,600');
  });

  it('can tell close millions apart', () => {
    expect(fmtUsdLarge(1_177_904, 2)).toBe('$1.18 million');
    expect(fmtUsdLarge(1_242_624, 2)).toBe('$1.24 million');
    expect(fmtUsdLarge(429_094, 2)).toBe('$429,094');
    expect(fmtUsdLarge(Number.NaN, 2)).toBe('–');
  });
});

describe('power and energy', () => {
  it('shows MW with a direction word; + is selling, - is charging', () => {
    expect(fmtPowerFlow(48_000)).toBe('48.0 MW selling');
    expect(fmtPowerFlow(-47_990.2)).toBe('48.0 MW charging');
    expect(fmtPowerFlow(0.4)).toBe('0 MW (waiting)');
    expect(fmtPowerFlow(0, 'kW')).toBe('0 kW (waiting)');
    expect(fmtPower(850)).toBe('850 kW');
    expect(powerDirection(-3)).toBe('charging');
  });

  it('translates power and energy into homes, worded as estimates', () => {
    expect(fmtHomesPower(48_000)).toBe('about what 32,000 homes use');
    expect(fmtHomesPower(1)).toBe('less than one home uses');
    expect(fmtEnergy(12_400)).toBe('12.4 MWh');
    expect(fmtEnergy(8_100)).toBe('8,100 kWh');
    expect(fmtHomesDay(12_400)).toBe('about what 340 homes use in a day');
  });

  it('rounds to significant figures', () => {
    expect(roundSignificant(32_456)).toBe(32_000);
    expect(roundSignificant(345)).toBe(350);
    expect(roundSignificant(0)).toBe(0);
  });
});

describe('speed', () => {
  it('says how long an hour of market time takes', () => {
    expect(fmtSpeed(2)).toBe('1 hour of market time every 6 seconds');
    expect(fmtSpeed(1)).toBe('1 hour of market time every 12 seconds');
    expect(fmtSpeed(5)).toBe('1 hour of market time every 2.4 seconds');
    expect(fmtSpeed(10)).toBe('1 hour of market time every 1.2 seconds');
    expect(fmtSpeedShort(2)).toBe('1 h every 6 s');
    expect(fmtSpeed(0)).toBe('Paused');
  });
});

describe('Central time', () => {
  it('formats the replay clock like the UX guide', () => {
    expect(fmtClock('2025-05-17T00:45:00Z')).toBe('May 16, 2025, 7:45 pm, Central time');
  });

  it('handles standard time (CST) as well as daylight time', () => {
    expect(fmtClock('2025-01-15T13:30:00Z')).toBe('Jan 15, 2025, 7:30 am, Central time');
  });

  it('drops :00 in compact times, and says midnight and noon in words', () => {
    expect(fmtTimeOfDay('2025-05-16T21:00:00Z', { compact: true })).toBe('4 pm');
    expect(fmtTimeOfDay('2025-05-16T21:00:00Z')).toBe('4:00 pm');
    expect(fmtTimeOfDay('2025-05-17T05:00:00Z', { compact: true })).toBe('12 am');
    expect(fmtTimeOfDay('2025-06-28T05:00:00Z', { compact: true, words: true })).toBe('midnight');
    expect(fmtTimeOfDay('2025-06-28T17:00:00Z', { compact: true, words: true })).toBe('noon');
    expect(
      fmtPresetLabel({
        id: 'ordinary_day',
        label: 'An ordinary day',
        date_local: '2025-06-28',
        start_utc: '2025-06-28T05:00:00Z',
        description: 'A typical day with small ups and downs.',
      }),
    ).toBe('An ordinary day, Jun 28, 2025, from midnight');
  });

  it('does not repeat a date the preset’s label already names ("From January 1")', () => {
    const jan1 = { id: 'jan1_2026', year: 2026, label: 'From January 1', date_local: '2026-01-01', description: '' };
    expect(fmtPresetLabel({ ...jan1, start_utc: '2026-01-01T06:00:00Z' })).toBe('From January 1, 2026');
    expect(fmtPresetLabel({ ...jan1, start_utc: '2026-01-01T12:00:00Z' })).toBe('From January 1, 2026, from 6 am');
    expect(fmtPresetLabel({ ...jan1, label: 'Where real prices end', date_local: '2026-09-20', start_utc: '2026-09-20T23:00:00Z' })).toBe(
      'Where real prices end, Sep 20, 2026, from 6 pm',
    );
  });

  it('reads calendar dates without shifting them a day', () => {
    expect(fmtLocalDate('2025-05-16')).toBe('May 16, 2025');
    expect(fmtLocalDate('2025-05-16', { weekday: true })).toBe('Fri, May 16, 2025');
    expect(fmtDate('2025-05-17T03:00:00Z')).toBe('May 16, 2025');
    expect(localDateOf('2025-05-17T03:00:00Z')).toBe('2025-05-16');
  });

  it('builds the preset label from the UX guide', () => {
    expect(
      fmtPresetLabel({
        id: 'spike_evening',
        label: 'Price-spike evening',
        date_local: '2025-05-16',
        start_utc: '2025-05-16T21:00:00Z',
        description: 'Prices jump above $2,000/MWh around 7:45 pm.',
      }),
    ).toBe('Price-spike evening, May 16, 2025, from 4 pm');
  });
});

describe('plain words', () => {
  it('names areas, methods and actions per the glossary', () => {
    expect(areaName('LZ_HOUSTON')).toBe('Houston area');
    expect(areaName('LZ_NORTH')).toBe('Dallas–Fort Worth area');
    expect(areaName('LZ_CPS')).toBe('San Antonio area');
    expect(areaName('LZ_AEN')).toBe('Austin area');
    expect(areaShortName('LZ_NORTH')).toBe('Dallas–Fort Worth');
    expect(areaName('HB_WEST')).toBe('HB_WEST');
    expect(fmtList(['Dallas–Fort Worth', 'Houston', 'San Antonio', 'Austin'])).toBe(
      'Dallas–Fort Worth, Houston, San Antonio, and Austin',
    );
    expect(strategyName('bands')).toBe('Simple rule');
    expect(strategyName('forecast')).toBe('AI forecast (TimesFM-3)');
    expect(strategyName('peak')).toBe('Evening peak');
    expect(actionLabel('charge')).toBe('Charging');
    expect(actionLabel('sell')).toBe('Selling');
    expect(actionLabel('hold')).toBe('Waiting');
  });

  it('shortens a method name for tight spaces and puts one inside a sentence', () => {
    expect(shortLabel('AI forecast (TimesFM-3)')).toBe('AI forecast');
    expect(shortLabel('Simple rule')).toBe('Simple rule');
    expect(shortLabel('(TimesFM-3)')).toBe('(TimesFM-3)');
    expect(lowerFirst('Simple rule')).toBe('simple rule');
    expect(lowerFirst('Evening peak')).toBe('evening peak');
    expect(lowerFirst('AI forecast')).toBe('AI forecast');
  });

  it('formats counts, percents, ranges and durations', () => {
    expect(fmtCount(20_000)).toBe('20,000');
    expect(fmtPercent(0.9)).toBe('90%');
    expect(fmtPercentValue(0.02)).toBe('0.02%');
    expect(fmtPercentValue(12.345)).toBe('12%');
    expect(fmtRange(5, 11.5)).toBe('5–11.5');
    expect(fmtDuration(65)).toBe('1 min 5 s');
    expect(fmtDuration(3600)).toBe('1 h');
  });
});
