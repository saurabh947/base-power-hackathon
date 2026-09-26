// Plain-language formatting. Every number the site shows goes through here so it
// carries a unit and, where it helps, a human translation (docs/UX.md principle 1):
// "$120/MWh (12¢ per kWh)", "48.0 MW (about what 32,000 homes use)".
// Market times are always shown in Central time (America/Chicago), which all four cities share.

import type { Preset, TickAction } from '../api/types.ts';
import { cityName } from './cities.ts';

export const LOCALE = 'en-US';
export const MARKET_TZ = 'America/Chicago';
/** What the site calls market time, as the backend's event messages do. */
export const TIME_ZONE_PHRASE = 'Central time';
export const TICK_MINUTES = 5;

/**
 * Estimate used only for human translations, and labelled "about": a typical
 * Texas home averages roughly 1.5 kW (about 1,100 kWh a month).
 */
export const HOME_AVG_KW = 1.5;
export const HOME_DAILY_KWH = HOME_AVG_KW * 24;

const MINUS = '−';

function withMinus(text: string): string {
  return text.replace('-', MINUS);
}

const numberFormats = new Map<string, Intl.NumberFormat>();
function nf(min: number, max: number): Intl.NumberFormat {
  const key = `${min}:${max}`;
  let f = numberFormats.get(key);
  if (!f) {
    f = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: min, maximumFractionDigits: max });
    numberFormats.set(key, f);
  }
  return f;
}

const usdFormats = new Map<number, Intl.NumberFormat>();
function usdFormat(decimals: number): Intl.NumberFormat {
  let f = usdFormats.get(decimals);
  if (!f) {
    f = new Intl.NumberFormat(LOCALE, {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
    usdFormats.set(decimals, f);
  }
  return f;
}

function finite(n: number | null | undefined): n is number {
  return typeof n === 'number' && Number.isFinite(n);
}

/** Round to `digits` significant figures (32,456 -> 32,000). */
export function roundSignificant(n: number, digits = 2): number {
  if (!finite(n) || n === 0) return 0;
  const magnitude = Math.floor(Math.log10(Math.abs(n)));
  const factor = 10 ** (magnitude - digits + 1);
  return Math.round(n / factor) * factor;
}

// ---- plain words -----------------------------------------------------------

/** "Houston area", "Dallas–Fort Worth area" (names from GET /fleet/layout); unknown ids as they are. */
export function areaName(location: string): string {
  const name = cityName(location);
  return name === location ? location : `${name} area`;
}

/** "Houston", "Dallas–Fort Worth" (for lists like "Dallas–Fort Worth, Houston, San Antonio, and Austin"). */
export function areaShortName(location: string): string {
  return cityName(location);
}

const listFormat = new Intl.ListFormat(LOCALE, { style: 'long', type: 'conjunction' });

/** "a, b, and c" */
export function fmtList(items: string[]): string {
  return listFormat.format(items);
}

/** "5–11.5" with an en dash; a single value when both ends match. */
export function fmtRange(low: number, high: number, decimals = 1): string {
  const f = (n: number) => withMinus(nf(0, decimals).format(n));
  return low === high ? f(low) : `${f(low)}–${f(high)}`;
}

const STRATEGY_NAMES: Record<string, string> = {
  bands: 'Simple rule',
  forecast: 'AI forecast (TimesFM-3)',
  peak: 'Evening peak',
};

const STRATEGY_TECH: Record<string, string> = {
  bands: 'Strategy v1, percentile bands',
  forecast: 'forecast strategy',
  peak: 'peak strategy',
};

export function strategyName(id: string): string {
  return STRATEGY_NAMES[id] ?? id;
}

export function strategyTechName(id: string): string {
  return STRATEGY_TECH[id] ?? id;
}

const ACTION_LABELS: Record<TickAction, string> = { charge: 'Charging', sell: 'Selling', hold: 'Waiting' };

export function actionLabel(action: TickAction | undefined | null): string {
  return action ? (ACTION_LABELS[action] ?? action) : 'No data';
}

export function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many;
}

// ---- numbers ---------------------------------------------------------------

export function fmtCount(n: number): string {
  return finite(n) ? withMinus(nf(0, 0).format(n)) : '–';
}

export function fmtNumber(n: number, decimals = 0): string {
  return finite(n) ? withMinus(nf(decimals, decimals).format(n)) : '–';
}

/** "$1,234" (2 decimals below $1,000 unless told otherwise). */
export function fmtUsd(n: number, decimals?: number): string {
  if (!finite(n)) return '–';
  const d = decimals ?? (Math.abs(n) < 1000 ? 2 : 0);
  return withMinus(usdFormat(d).format(n));
}

/**
 * "$3.2 million", "$1.25 billion"; plain dollars below a million. `millionDecimals` tells close sums apart,
 * e.g. "$1.18 million" and "$1.24 million" in a list where both would read "$1.2 million".
 */
export function fmtUsdLarge(n: number, millionDecimals = 1): string {
  if (!finite(n)) return '–';
  const abs = Math.abs(n);
  if (abs >= 1e9) return `${fmtUsd(n / 1e9, 2)} billion`;
  if (abs >= 1e6) return `${fmtUsd(n / 1e6, millionDecimals)} million`;
  return fmtUsd(n, 0);
}

/** "90%" from a 0-1 fraction. */
export function fmtPercent(fraction: number, decimals = 0): string {
  return finite(fraction) ? `${fmtNumber(fraction * 100, decimals)}%` : '–';
}

/** error_pct arrives already in percent (0.02 means 0.02%). */
export function fmtPercentValue(pct: number): string {
  if (!finite(pct)) return '–';
  const abs = Math.abs(pct);
  const d = abs < 1 ? 2 : abs < 10 ? 1 : 0;
  return `${fmtNumber(pct, d)}%`;
}

// ---- prices ------------------------------------------------------------------

/** "$120/MWh", "$42.10/MWh", "−$5.00/MWh". */
export function fmtPriceMwh(usdPerMwh: number): string {
  if (!finite(usdPerMwh)) return '–';
  return `${fmtUsd(usdPerMwh, Math.abs(usdPerMwh) >= 100 ? 0 : 2)}/MWh`;
}

/** The same price per kWh, the unit on a home electricity bill: "12¢ per kWh", "$2.25 per kWh". */
export function fmtPriceKwh(usdPerMwh: number): string {
  if (!finite(usdPerMwh)) return '–';
  const cents = usdPerMwh / 10;
  const abs = Math.abs(cents);
  if (abs >= 100) return `${fmtUsd(cents / 100, 2)} per kWh`;
  const d = abs >= 10 ? 0 : 1;
  const digits = nf(d, d).format(abs);
  const sign = cents < 0 && Number(digits) !== 0 ? MINUS : '';
  return `${sign}${digits}¢ per kWh`;
}

/** The per-kWh translation for tight spaces: "12¢/kWh", "$1.76/kWh". */
export function fmtPriceKwhShort(usdPerMwh: number): string {
  if (!finite(usdPerMwh)) return '–';
  return fmtPriceKwh(usdPerMwh).replace(' per kWh', '/kWh');
}

/** "$120/MWh (12¢ per kWh)" */
export function fmtPrice(usdPerMwh: number): string {
  if (!finite(usdPerMwh)) return '–';
  return `${fmtPriceMwh(usdPerMwh)} (${fmtPriceKwh(usdPerMwh)})`;
}

// ---- power and energy -------------------------------------------------------

/** Magnitude only: "48.0 MW", "850 kW". Direction words come from powerDirection(). */
export function fmtPower(kw: number): string {
  if (!finite(kw)) return '–';
  const abs = Math.abs(kw);
  if (abs >= 1000) return `${nf(1, 1).format(abs / 1000)} MW`;
  return `${nf(0, 0).format(abs)} kW`;
}

export type PowerDirection = 'selling' | 'charging' | 'idle';

/** + is selling to the grid, − is charging (docs/API.md). Below 1 kW counts as idle. */
export function powerDirection(kw: number): PowerDirection {
  if (!finite(kw) || Math.abs(kw) < 1) return 'idle';
  return kw > 0 ? 'selling' : 'charging';
}

/** "48.0 MW selling", "12.3 MW charging", "0 MW (waiting)"; use idleUnit "kW" for a single battery. */
export function fmtPowerFlow(kw: number, idleUnit: 'MW' | 'kW' = 'MW'): string {
  if (!finite(kw)) return '–';
  const dir = powerDirection(kw);
  if (dir === 'idle') return `0 ${idleUnit} (waiting)`;
  return `${fmtPower(kw)} ${dir}`;
}

/** "about what 32,000 homes use" — an estimate from HOME_AVG_KW, always worded as one. */
export function fmtHomesPower(kw: number): string {
  if (!finite(kw)) return '';
  const homes = roundSignificant(Math.abs(kw) / HOME_AVG_KW, 2);
  if (homes < 1) return 'less than one home uses';
  return `about what ${fmtCount(homes)} ${plural(homes, 'home uses', 'homes use')}`;
}

/** "12.4 MWh" from 10,000 kWh up, "8,100 kWh" below. */
export function fmtEnergy(kwh: number): string {
  if (!finite(kwh)) return '–';
  const abs = Math.abs(kwh);
  if (abs >= 10_000) return withMinus(`${nf(1, 1).format(kwh / 1000)} MWh`);
  return withMinus(`${nf(0, 0).format(kwh)} kWh`);
}

/** "about what 340 homes use in a day" — an estimate, always worded as one. */
export function fmtHomesDay(kwh: number): string {
  if (!finite(kwh)) return '';
  const homes = roundSignificant(Math.abs(kwh) / HOME_DAILY_KWH, 2);
  if (homes < 1) return 'less than one home uses in a day';
  return `about what ${fmtCount(homes)} ${plural(homes, 'home uses', 'homes use')} in a day`;
}

// ---- speed -----------------------------------------------------------------

/** speed_tps is 5-minute steps per wall-clock second. */
export function secondsPerMarketHour(speedTps: number): number {
  return 60 / TICK_MINUTES / speedTps;
}

/** "1 hour of market time every 6 seconds" */
export function fmtSpeed(speedTps: number): string {
  if (!finite(speedTps) || speedTps <= 0) return 'Paused';
  const s = secondsPerMarketHour(speedTps);
  return `1 hour of market time every ${nf(0, 1).format(s)} ${s === 1 ? 'second' : 'seconds'}`;
}

/** "1 h every 6 s" for tight spaces. */
export function fmtSpeedShort(speedTps: number): string {
  if (!finite(speedTps) || speedTps <= 0) return 'Paused';
  return `1 h every ${nf(0, 1).format(secondsPerMarketHour(speedTps))} s`;
}

export function fmtDuration(totalSeconds: number): string {
  if (!finite(totalSeconds)) return '–';
  const s = Math.max(0, Math.round(totalSeconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m} min ${s % 60} s` : `${m} min`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
}

// ---- dates and times (Central time) -------------------------------------------

const timeFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: MARKET_TZ,
  hour: 'numeric',
  minute: '2-digit',
  hour12: true,
});
const dateFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: MARKET_TZ,
  month: 'short',
  day: 'numeric',
  year: 'numeric',
});
const utcDateFormat = new Intl.DateTimeFormat(LOCALE, { timeZone: 'UTC', month: 'short', day: 'numeric', year: 'numeric' });
const utcWeekdayDateFormat = new Intl.DateTimeFormat(LOCALE, {
  timeZone: 'UTC',
  weekday: 'short',
  month: 'short',
  day: 'numeric',
  year: 'numeric',
});
const localDayFormat = new Intl.DateTimeFormat('en-CA', {
  timeZone: MARKET_TZ,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

function toDate(value: string | number | Date): Date | null {
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * "7:45 pm"; with `compact`, whole hours drop the minutes: "4 pm"; with `words` as well,
 * 12 am and 12 pm read "midnight" and "noon".
 */
export function fmtTimeOfDay(value: string | number | Date, options: { compact?: boolean; words?: boolean } = {}): string {
  const d = toDate(value);
  if (!d) return '–';
  const parts = timeFormat.formatToParts(d);
  const get = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? '';
  const hour = get('hour');
  const minute = get('minute');
  const period = get('dayPeriod').toLowerCase();
  if (options.compact && minute === '00') {
    if (options.words && hour === '12') return period === 'am' ? 'midnight' : 'noon';
    return `${hour} ${period}`;
  }
  return `${hour}:${minute} ${period}`;
}

/** "May 16, 2025" for an instant, as a Central-time calendar date. */
export function fmtDate(value: string | number | Date): string {
  const d = toDate(value);
  return d ? dateFormat.format(d) : '–';
}

/** The replay clock: "May 16, 2025, 7:45 pm, Central time" (all four cities keep America/Chicago time). */
export function fmtClock(value: string | number | Date): string {
  const d = toDate(value);
  if (!d) return '–';
  return `${fmtDate(d)}, ${fmtTimeOfDay(d)}, ${TIME_ZONE_PHRASE}`;
}

/** Central-time calendar date of an instant as YYYY-MM-DD. */
export function localDateOf(value: string | number | Date): string | null {
  const d = toDate(value);
  return d ? localDayFormat.format(d) : null;
}

/** A calendar date string ("2025-05-16") as "May 16, 2025" (or "Fri, May 16, 2025"). No timezone shift. */
export function fmtLocalDate(dateLocal: string, options: { weekday?: boolean } = {}): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateLocal);
  if (!m) return dateLocal;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  return (options.weekday ? utcWeekdayDateFormat : utcDateFormat).format(d);
}

/** "Price-spike evening, May 16, 2025, from 4 pm" */
export function fmtPresetLabel(preset: Preset): string {
  const date = preset.date_local ?? (preset.start_utc ? localDateOf(preset.start_utc) : null);
  const bits = [preset.label];
  if (date) bits.push(fmtLocalDate(date));
  if (preset.start_utc) bits.push(`from ${fmtTimeOfDay(preset.start_utc, { compact: true, words: true })}`);
  return bits.join(', ');
}
