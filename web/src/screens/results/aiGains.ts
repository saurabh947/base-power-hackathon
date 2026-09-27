// "What the AI adds" on the Results page: its headline (what the AI forecast earns over the simple rule) and
// its surplus-power figure, from GET /results/ai-gains?year= (docs/API.md); the methods' bars beside them come
// from the results (./model.ts). Pure, so the uplift maths and the wording are unit tested.
//
// It shows the year the Results page shows (the run's, or the one picked on Start): the entry of `years`
// for that year. Without a year it shows the pitch year (the first entry with `is_pitch: true`). Other years
// the API may send are ignored. Nothing here is hardcoded: every figure comes from the API. For a year with
// an AI-predicted part (2026), the uplift is split into its real and projected parts using the Results'
// real_per_battery_usd and projected_per_battery_usd.

import type { AiGainsYear, StrategyGains, Uplift } from '../../api/types.ts';
import { fmtCount, fmtHomesDay, fmtNumber, fmtPercent, fmtUsd, fmtUsdLarge, lowerFirst, shortLabel, strategyName } from '../../lib/format.ts';
import { roundedParts, type Split, type YearSplit } from './model.ts';

/** GET /results/ai-gains as the site uses it: the AI, the baseline, and the year shown. */
export interface PitchGains {
  ai: string;
  baseline: string;
  year: AiGainsYear;
}

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
 * GET /results/ai-gains, read defensively. With `year`, the entry for that year (none: null, so a backend that
 * ignores ?year= never shows another year's numbers as this one's). Without it, the pitch year: the first entry
 * with `is_pitch: true`, or with none flagged (older data) the first usable one. Null (the section is hidden)
 * when that year lacks the AI or the baseline, or nothing usable is there.
 */
export function readAiGains(raw: unknown, wanted: number | null = null): PitchGains | null {
  if (!isObject(raw) || !Array.isArray(raw.years)) return null;
  const ai = typeof raw.ai === 'string' && raw.ai ? raw.ai : 'forecast';
  const baseline = typeof raw.baseline === 'string' && raw.baseline ? raw.baseline : 'bands';
  if (wanted !== null) {
    const entry = raw.years.find((y) => isObject(y) && y.year === wanted);
    const year = entry === undefined ? null : readYear(entry, ai, baseline);
    return year ? { ai, baseline, year } : null;
  }
  const pitch = raw.years.find((y) => isObject(y) && y.is_pitch === true);
  if (pitch !== undefined) {
    const year = readYear(pitch, ai, baseline);
    return year ? { ai, baseline, year } : null;
  }
  for (const y of raw.years) {
    const year = readYear(y, ai, baseline);
    if (year) return { ai, baseline, year };
  }
  return null;
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

export function gainsHeadline(gains: PitchGains, year: AiGainsYear, knownFleetSize: number | null): GainsHeadline | null {
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

// ---- surplus power -----------------------------------------------------------------------------------------

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

/** Soaks up surplus power: energy bought at prices at or below $0/MWh. */
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

export function surplusCard(gains: PitchGains, year: AiGainsYear): SurplusCard | null {
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

// ---- a year with an AI-predicted part (2026) --------------------------------------------------------------

/** From GET /results/pitch: where the real prices end, and the AI's and the baseline's real and projected parts. */
export interface SplitInput {
  span: YearSplit;
  ai: Split;
  base: Split;
}

/** "Real Jan 1 – Sep 20: +$45 per battery · Projected Sep 21 – Dec 31: +$25 · 2026 estimate: +$70", in parts. */
export interface GainsSplit {
  real: string;
  projected: string;
  estimate: string;
}

/** Whole dollars with a sign: "+$45", "−$3". */
const signedUsd = (n: number) => `${n < 0 ? MINUS : '+'}${fmtUsd(Math.abs(n), 0)}`;

export function gainsSplit(input: SplitInput | null, year: number): GainsSplit | null {
  if (!input) return null;
  // Whole dollars that add up everywhere: each side's parts add up to its total (roundedParts), and the gains
  // are the differences of those same shown numbers.
  const [aiReal, aiProjected, aiTotal] = roundedParts(input.ai.real, input.ai.projected);
  const [baseReal, baseProjected, baseTotal] = roundedParts(input.base.real, input.base.projected);
  return {
    real: `Real ${input.span.realSpan}: ${signedUsd(aiReal - baseReal)} per battery`,
    projected: `Projected ${input.span.projectedSpan}: ${signedUsd(aiProjected - baseProjected)}`,
    estimate: `${year} estimate: ${signedUsd(aiTotal - baseTotal)}`,
  };
}

// ---- everything the section shows -------------------------------------------------------------------------

export interface GainsView {
  year: AiGainsYear;
  headline: GainsHeadline | null;
  surplus: SurplusCard | null;
  /** A year with an AI-predicted part: the uplift split into real and projected; null for a year of real prices. */
  split: GainsSplit | null;
}

/** The headline and the surplus figure, for the year shown (and its real/projected split, when it has one). */
export function gainsView(gains: PitchGains, knownFleetSize: number | null, splitInput: SplitInput | null = null): GainsView {
  const y = gains.year;
  return {
    year: y,
    headline: gainsHeadline(gains, y, knownFleetSize),
    surplus: surplusCard(gains, y),
    split: gainsSplit(splitInput, y.year),
  };
}
