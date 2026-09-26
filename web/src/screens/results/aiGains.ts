// "What the AI adds" on the Results page, from GET /results/ai-gains (docs/API.md). Pure, so the uplift
// maths, the wording, the year switch and the spike card (always 2024) are unit tested.
//
// The headline and the first three cards follow the year switch; the spike card always shows 2024, the
// year with many price spikes, and says so. Nothing here is hardcoded: every figure comes from the API.

import type { AiGains, AiGainsYear, StrategyGains, Uplift } from '../../api/types.ts';
import {
  fmtCount,
  fmtHomesDay,
  fmtNumber,
  fmtPercent,
  fmtPriceKwh,
  fmtPriceMwh,
  fmtUsd,
  fmtUsdLarge,
  plural,
  shortLabel,
  strategyName,
} from '../../lib/format.ts';

/** The spike card always shows this year (the human's choice): 2024 had many price spikes. */
export const SPIKE_YEAR = 2024;
/** docs/API.md: spikes are 5-minute prices at or above $1,000/MWh. */
export const SPIKE_PRICE_USD_MWH = 1000;

type Json = Record<string, unknown>;

function isObject(v: unknown): v is Json {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function finite(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

/** A number from the wire, or NaN (which every formatter shows as "–"). */
function num(v: unknown): number {
  return finite(v) ? v : Number.NaN;
}

const GAINS_FIELDS = [
  'per_battery_usd_year',
  'fleet_usd_year',
  'share_of_best',
  'surplus_mwh',
  'spike_mwh',
  'spike_events',
  'spike_charge_level',
  'sold_mwh',
  'bought_mwh',
  'avg_sell_usd_mwh',
  'avg_buy_usd_mwh',
] as const;

function readStrategyGains(id: string, v: unknown): StrategyGains | null {
  if (!isObject(v) || !finite(v.per_battery_usd_year)) return null;
  const out = { label: typeof v.label === 'string' && v.label ? v.label : strategyName(id) } as StrategyGains;
  for (const key of GAINS_FIELDS) out[key] = num(v[key]);
  return out;
}

function readYear(v: unknown, ai: string, baseline: string): AiGainsYear | null {
  if (!isObject(v) || !finite(v.year) || !isObject(v.strategies)) return null;
  const strategies: Record<string, StrategyGains> = {};
  for (const [id, s] of Object.entries(v.strategies)) {
    const read = readStrategyGains(id, s);
    if (read) strategies[id] = read;
  }
  // Without both the AI and the baseline there is nothing to compare.
  if (!strategies[ai] || !strategies[baseline]) return null;
  const u = isObject(v.uplift) ? v.uplift : {};
  return {
    year: Math.round(v.year),
    is_pitch: v.is_pitch === true,
    best_possible_per_battery_usd_year: num(v.best_possible_per_battery_usd_year),
    strategies,
    uplift: { per_battery_usd_year: num(u.per_battery_usd_year), pct: num(u.pct), fleet_usd_year: num(u.fleet_usd_year) },
  };
}

/**
 * GET /results/ai-gains, read defensively: years without the AI or the baseline are skipped, a repeated
 * year keeps its first entry, and the pitch year comes first. Null when nothing usable is left.
 */
export function readAiGains(raw: unknown): AiGains | null {
  if (!isObject(raw) || !Array.isArray(raw.years)) return null;
  const ai = typeof raw.ai === 'string' && raw.ai ? raw.ai : 'forecast';
  const baseline = typeof raw.baseline === 'string' && raw.baseline ? raw.baseline : 'bands';
  const years: AiGainsYear[] = [];
  for (const y of raw.years) {
    const read = readYear(y, ai, baseline);
    if (read && !years.some((x) => x.year === read.year)) years.push(read);
  }
  if (!years.length) return null;
  // Stable: the pitch year first, then the others in the server's order.
  return { ai, baseline, years: years.toSorted((a, b) => Number(b.is_pitch) - Number(a.is_pitch)) };
}

// ---- the year switch ----------------------------------------------------------------------------------

export interface YearOption {
  year: number;
  /** "2025 (headline year)", "2024 (check year)". */
  label: string;
  isPitch: boolean;
}

/** One option per year, the pitch year first. On a phone a label breaks after the year, never inside the note. */
export function yearOptions(gains: AiGains): YearOption[] {
  return gains.years.map((y) => ({
    year: y.year,
    label: `${y.year} (${y.is_pitch ? 'headline year' : 'check year'})`,
    isPitch: y.is_pitch,
  }));
}

/** The chosen year's entry; the pitch year (or the first) when that year is not in the data. */
export function pickYear(gains: AiGains, year: number | null): AiGainsYear {
  return gains.years.find((y) => y.year === year) ?? gains.years.find((y) => y.is_pitch) ?? gains.years[0];
}

/** "The headline number above is for 2025. 2024 is a second full year, to check that the gain holds." */
export function yearsNote(gains: AiGains): string | null {
  const pitch = gains.years.find((y) => y.is_pitch);
  const others = gains.years.filter((y) => y !== pitch).map((y) => y.year);
  if (!pitch || !others.length) return null;
  const list = others.length === 1 ? String(others[0]) : `${others.slice(0, -1).join(', ')} and ${others.at(-1)}`;
  return `The headline number above is for ${pitch.year}. ${list} ${others.length === 1 ? 'is a second full year' : 'are other full years'}, to check that the gain holds.`;
}

// ---- formatting -------------------------------------------------------------------------------------------

const MINUS = '−';

/** "+$67", "−$12"; cents below $10 ("+$0.40"). */
export function fmtSignedUsd(n: number): string {
  if (!finite(n)) return '–';
  const text = fmtUsd(Math.abs(n), Math.abs(n) < 10 ? 2 : 0);
  return `${n < 0 ? MINUS : '+'}${text}`;
}

/** "+54%", "+139%", "−8%" from a fraction (0.544 is +54%). */
export function fmtSignedPct(fraction: number): string {
  if (!finite(fraction)) return '–';
  return `${fraction < 0 ? MINUS : '+'}${fmtPercent(Math.abs(fraction), 0)}`;
}

/** How many times as much: "5.7×", "8×", "12×". Null when there is nothing to compare with. */
export function fmtTimes(a: number, b: number): string | null {
  if (!finite(a) || !finite(b) || a <= 0 || b <= 0) return null;
  const r = a / b;
  const text = r >= 10 ? fmtNumber(r, 0) : fmtNumber(r, 1).replace(/\.0$/, '');
  return `${text}×`;
}

/** "7,011 MWh"; one decimal below 10 MWh. */
export function fmtMwh(mwh: number): string {
  if (!finite(mwh)) return '–';
  return `${fmtNumber(mwh, Math.abs(mwh) < 10 ? 1 : 0)} MWh`;
}

/** "Simple rule" → "simple rule" (but "AI forecast" stays): for "vs the simple rule". */
function lowerFirst(label: string): string {
  return /^[A-Z][a-z]/.test(label) ? label[0].toLowerCase() + label.slice(1) : label;
}

/** A 0-1 bar length. */
const clampShare = (v: number) => (finite(v) ? Math.min(1, Math.max(0, v)) : 0);

// ---- the headline -----------------------------------------------------------------------------------------

/**
 * The fleet the gains are for: the pitch results' fleet_size when known, else what the AI's own numbers
 * imply (fleet ÷ per battery). Null when neither is there.
 */
export function fleetSizeOf(year: AiGainsYear, ai: string, known: number | null | undefined): number | null {
  if (finite(known) && known > 0) return known;
  for (const s of [year.strategies[ai], ...Object.values(year.strategies)]) {
    if (s && finite(s.fleet_usd_year) && finite(s.per_battery_usd_year) && s.per_battery_usd_year > 0) {
      return Math.round(s.fleet_usd_year / s.per_battery_usd_year);
    }
  }
  return null;
}

export interface UpliftNumbers {
  /** Dollars per battery per year. */
  perBattery: number;
  /** A fraction of the baseline's earnings (0.544 is +54%); null when the baseline earned nothing. */
  pct: number | null;
  /** Dollars a year for the whole fleet; null when the fleet size is unknown. */
  fleet: number | null;
}

/** From `uplift`, filling any missing number from the two strategies and the fleet size. */
export function upliftOf(year: AiGainsYear, ai: string, baseline: string, fleetSize: number | null): UpliftNumbers | null {
  const a = year.strategies[ai];
  const b = year.strategies[baseline];
  const u: Uplift = year.uplift;
  const perBattery = finite(u.per_battery_usd_year) ? u.per_battery_usd_year : a && b ? a.per_battery_usd_year - b.per_battery_usd_year : Number.NaN;
  if (!finite(perBattery)) return null;
  const basePer = b?.per_battery_usd_year;
  const pct = finite(u.pct) ? u.pct : finite(basePer) && basePer > 0 ? perBattery / basePer : null;
  const fleet = finite(u.fleet_usd_year) ? u.fleet_usd_year : fleetSize !== null ? perBattery * fleetSize : null;
  return { perBattery, pct, fleet };
}

export interface GainsHeadline extends UpliftNumbers {
  /** "+$67" */
  amount: string;
  /** "+54%" */
  pctText: string | null;
  /** "vs the simple rule" */
  versus: string;
  /** "$1.35 million more a year for the 20,000-battery fleet" */
  fleetText: string | null;
  /** The whole line: "+$67 per battery per year (+54%) vs the simple rule, $1.35 million more a year for the 20,000-battery fleet". */
  sentence: string;
}

export function gainsHeadline(gains: AiGains, year: AiGainsYear, knownFleetSize: number | null): GainsHeadline | null {
  const fleetSize = fleetSizeOf(year, gains.ai, knownFleetSize);
  const up = upliftOf(year, gains.ai, gains.baseline, fleetSize);
  if (!up) return null;
  const amount = fmtSignedUsd(up.perBattery);
  const pctText = up.pct === null ? null : fmtSignedPct(up.pct);
  const versus = `vs the ${lowerFirst(shortLabel(year.strategies[gains.baseline].label))}`;
  const fleetText =
    up.fleet === null
      ? null
      : `${fmtUsdLarge(Math.abs(up.fleet), 2)} ${up.fleet < 0 ? 'less' : 'more'} a year for the ${fleetSize === null ? 'fleet' : `${fmtCount(fleetSize)}-battery fleet`}`;
  const sentence = `${amount} per battery per year${pctText ? ` (${pctText})` : ''} ${versus}${fleetText ? `, ${fleetText}` : ''}`;
  return { ...up, amount, pctText, versus, fleetText, sentence };
}

// ---- the cards ---------------------------------------------------------------------------------------------

export interface Pair {
  /** The AI's value and the baseline's, as bar lengths against the larger (0-1). */
  aiShare: number;
  baseShare: number;
}

function pair(ai: number, base: number): Pair {
  const max = Math.max(finite(ai) ? ai : 0, finite(base) ? base : 0);
  return { aiShare: max > 0 ? clampShare(ai / max) : 0, baseShare: max > 0 ? clampShare(base / max) : 0 };
}

export interface Side {
  label: string;
  /** Short name for bars and tight spaces: "AI forecast". */
  short: string;
}

function side(year: AiGainsYear, id: string): Side {
  const label = year.strategies[id]?.label || strategyName(id);
  return { label, short: shortLabel(label) };
}

/** (a) Earns more: the AI vs the simple rule per battery, and the fleet difference. */
export interface EarnsCard extends Pair {
  ai: Side;
  base: Side;
  /** "$191" and "$124" per battery per year. */
  aiText: string;
  baseText: string;
  /** "$3.82 million vs $2.47 million a year" */
  fleetText: string | null;
  /** "+$1.35 million a year" */
  fleetDiffText: string | null;
}

export function earnsCard(gains: AiGains, year: AiGainsYear, knownFleetSize: number | null): EarnsCard | null {
  const a = year.strategies[gains.ai];
  const b = year.strategies[gains.baseline];
  if (!finite(a.per_battery_usd_year) || !finite(b.per_battery_usd_year)) return null;
  const up = upliftOf(year, gains.ai, gains.baseline, fleetSizeOf(year, gains.ai, knownFleetSize));
  const bothFleets = finite(a.fleet_usd_year) && finite(b.fleet_usd_year);
  return {
    ai: side(year, gains.ai),
    base: side(year, gains.baseline),
    aiText: fmtUsd(a.per_battery_usd_year, 0),
    baseText: fmtUsd(b.per_battery_usd_year, 0),
    ...pair(a.per_battery_usd_year, b.per_battery_usd_year),
    fleetText: bothFleets ? `${fmtUsdLarge(a.fleet_usd_year, 2)} vs ${fmtUsdLarge(b.fleet_usd_year, 2)} a year` : null,
    fleetDiffText:
      up && up.fleet !== null ? `${up.fleet < 0 ? MINUS : '+'}${fmtUsdLarge(Math.abs(up.fleet), 2)} a year` : null,
  };
}

/** (b) Gets closer to perfect: each method's share of the hindsight best, as bars. */
export interface PerfectBar {
  key: string;
  label: string;
  /** 0-1 of "perfect", for the bar. */
  share: number;
  /** "45%" */
  text: string;
  ai: boolean;
}

export interface PerfectCard {
  /** The simple rule first, then the other methods, the AI last. */
  bars: PerfectBar[];
  /** "$421" per battery per year: what 100% is. */
  bestText: string | null;
}

export function perfectCard(gains: AiGains, year: AiGainsYear): PerfectCard | null {
  const best = year.best_possible_per_battery_usd_year;
  const others = Object.keys(year.strategies)
    .filter((id) => id !== gains.ai && id !== gains.baseline)
    .toSorted((x, y) => (x === 'peak' ? -1 : y === 'peak' ? 1 : x.localeCompare(y)));
  const bars: PerfectBar[] = [];
  for (const id of [gains.baseline, ...others, gains.ai]) {
    const s = year.strategies[id];
    const share = finite(s.share_of_best) ? s.share_of_best : finite(best) && best > 0 ? s.per_battery_usd_year / best : Number.NaN;
    if (!finite(share)) continue;
    bars.push({ key: id, label: side(year, id).short, share: clampShare(share), text: fmtPercent(share, 0), ai: id === gains.ai });
  }
  if (!bars.some((b) => b.ai)) return null;
  return { bars, bestText: finite(best) && best > 0 ? fmtUsd(best, 0) : null };
}

/** (c) Soaks up surplus power: energy bought at prices at or below $0/MWh. */
export interface SurplusCard extends Pair {
  ai: Side;
  base: Side;
  /** "7,011 MWh" and "1,239 MWh". */
  aiText: string;
  baseText: string;
  /** "5.7×" */
  times: string | null;
  /** "about what 190,000 homes use in a day" (the AI's). */
  homes: string;
}

export function surplusCard(gains: AiGains, year: AiGainsYear): SurplusCard | null {
  const a = year.strategies[gains.ai].surplus_mwh;
  const b = year.strategies[gains.baseline].surplus_mwh;
  if (!finite(a) || !finite(b)) return null;
  return {
    ai: side(year, gains.ai),
    base: side(year, gains.baseline),
    aiText: fmtMwh(a),
    baseText: fmtMwh(b),
    ...pair(a, b),
    times: fmtTimes(a, b),
    homes: fmtHomesDay(a * 1000),
  };
}

/** (d) Ready for spikes: always 2024, labelled with its year; null (card hidden) without a 2024 entry. */
export interface SpikeCard {
  year: number;
  ai: Side;
  base: Side;
  /** 0-1 charge levels when the spikes began, averaged. */
  aiCharge: number;
  baseCharge: number;
  /** "52%" and "24%" */
  aiChargeText: string;
  baseChargeText: string;
  /** "611 MWh" and "76 MWh" sold during the spikes. */
  aiMwhText: string;
  baseMwhText: string;
  /** "8×" */
  times: string | null;
  /** "2024 had 53 price spikes of $1,000/MWh or more ($1.00 per kWh)." Null when the count is missing. */
  eventsText: string | null;
}

export function spikeCard(gains: AiGains): SpikeCard | null {
  const year = gains.years.find((y) => y.year === SPIKE_YEAR);
  if (!year) return null;
  const a = year.strategies[gains.ai];
  const b = year.strategies[gains.baseline];
  if (![a.spike_charge_level, b.spike_charge_level, a.spike_mwh, b.spike_mwh].every(finite)) return null;
  const events = finite(a.spike_events) ? a.spike_events : b.spike_events;
  if (finite(events) && events <= 0) return null;
  return {
    year: year.year,
    ai: side(year, gains.ai),
    base: side(year, gains.baseline),
    aiCharge: clampShare(a.spike_charge_level),
    baseCharge: clampShare(b.spike_charge_level),
    aiChargeText: fmtPercent(a.spike_charge_level, 0),
    baseChargeText: fmtPercent(b.spike_charge_level, 0),
    aiMwhText: fmtMwh(a.spike_mwh),
    baseMwhText: fmtMwh(b.spike_mwh),
    times: fmtTimes(a.spike_mwh, b.spike_mwh),
    eventsText: finite(events)
      ? `${year.year} had ${fmtCount(events)} price ${plural(events, 'spike', 'spikes')} of ${fmtPriceMwh(SPIKE_PRICE_USD_MWH)} or more (${fmtPriceKwh(SPIKE_PRICE_USD_MWH)}).`
      : null,
  };
}

// ---- everything the section shows for one position of the switch ---------------------------------------------------

export interface GainsView {
  year: AiGainsYear;
  headline: GainsHeadline | null;
  earns: EarnsCard | null;
  perfect: PerfectCard | null;
  surplus: SurplusCard | null;
  /** Always 2024, whatever the switch says. */
  spike: SpikeCard | null;
}

export function gainsView(gains: AiGains, year: number | null, knownFleetSize: number | null): GainsView {
  const y = pickYear(gains, year);
  return {
    year: y,
    headline: gainsHeadline(gains, y, knownFleetSize),
    earns: earnsCard(gains, y, knownFleetSize),
    perfect: perfectCard(gains, y),
    surplus: surplusCard(gains, y),
    spike: spikeCard(gains),
  };
}
