// What the Results panels draw, from GET /results/pitch?year= (docs/API.md). Pure, so
// the order of the rows, the lengths of the bars and the real/projected split are unit tested.
//
// A year with an AI-predicted part (2026) carries real_until_utc, and every figure splits into what real
// prices earned up to then and what the AI's predicted prices project for the rest of the year.

import type { Results, StrategyResult } from '../../api/types.ts';
import { cityName } from '../../lib/cities.ts';
import { fmtLocalDate, localDateOf, strategyName } from '../../lib/format.ts';

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

export interface YearSplit {
  /** The last day of real prices and the first of the AI's prediction, Central time ("2026-09-20"). */
  lastRealDay: string;
  firstProjectedDay: string;
  /** "Jan 1 – Sep 20" */
  realSpan: string;
  /** "Sep 21 – Dec 31" */
  projectedSpan: string;
}

const dayMonth = (day: string) => fmtLocalDate(day).replace(/, \d{4}$/, '');

/** Where a year's real prices end and the AI's prediction takes over; null for a year of real prices only. */
export function yearSplit(results: Pick<Results, 'year' | 'real_until_utc'>): YearSplit | null {
  const until = results.real_until_utc ? Date.parse(results.real_until_utc) : Number.NaN;
  if (!Number.isFinite(until)) return null;
  const lastRealDay = localDateOf(until - 1);
  const firstProjectedDay = localDateOf(until);
  const year = String(results.year);
  // Only a prediction that starts inside the year splits it.
  if (!lastRealDay?.startsWith(year) || !firstProjectedDay?.startsWith(year)) return null;
  return {
    lastRealDay,
    firstProjectedDay,
    realSpan: `Jan 1 – ${dayMonth(lastRealDay)}`,
    projectedSpan: `${dayMonth(firstProjectedDay)} – Dec 31`,
  };
}

/** A per-battery figure's parts: earned on real prices, and projected on the AI's. */
export interface Split {
  real: number;
  projected: number;
  total: number;
}

/** The real and projected parts of a strategy's or the ceiling's figure; null when the backend does not split it. */
export function splitOf(s: { per_battery_usd_year: number; real_per_battery_usd?: number; projected_per_battery_usd?: number }): Split | null {
  const { real_per_battery_usd: real, projected_per_battery_usd: projected } = s;
  if (!finite(real) || !finite(projected)) return null;
  return { real, projected, total: finite(s.per_battery_usd_year) ? s.per_battery_usd_year : real + projected };
}

/**
 * Whole dollars for two parts and their sum, so the shown parts add up to the shown total ("$151 + $54 =
 * $205", not "$152 + $54 = $205"): each part is rounded, and when they miss the rounded total by a dollar, the
 * part that was rounded the furthest gives or takes it.
 */
export function roundedParts(a: number, b: number): [number, number, number] {
  const total = Math.round(a + b);
  let ra = Math.round(a);
  let rb = Math.round(b);
  const over = ra + rb - total;
  if (over > 0) {
    if (ra - a >= rb - b) ra -= over;
    else rb -= over;
  } else if (over < 0) {
    if (a - ra >= b - rb) ra -= over;
    else rb -= over;
  }
  return [ra, rb, total];
}

/** Is this day (YYYY-MM-DD, Central time) in the AI-predicted part of the year? */
export function isProjectedDay(dateLocal: string, split: YearSplit | null): boolean {
  return split !== null && dateLocal >= split.firstProjectedDay;
}

/** Simple rule, AI forecast, Evening peak; anything else after them. */
const ORDER = ['bands', 'forecast', 'peak'];

export function strategyLabel(s: StrategyResult): string {
  return s.label || strategyName(s.strategy);
}

function rank(s: StrategyResult): number {
  const i = ORDER.indexOf(s.strategy);
  return i === -1 ? ORDER.length : i;
}

/** The strategy the big number shows: the one marked as the headline, else the first. */
export function headlineOf(results: Results | null): StrategyResult | null {
  return results?.strategies.find((s) => s.is_headline) ?? results?.strategies[0] ?? null;
}

export interface BarRow {
  key: string;
  label: string;
  /** Dollars per battery per year. */
  value: number;
  /** Bar length, 0-1 of the longest bar. */
  share: number;
  /** A method's share of the best possible (0.49 is 49%); null for the ceiling itself, or without one. */
  ofBest: number | null;
  /** The method of the big number (green). */
  headline: boolean;
  /** "Best possible (perfect hindsight)" (hatched). */
  ceiling: boolean;
  /** In a year with an AI-predicted part: the real and projected parts, and their bar lengths (they add up to `share`). */
  split: (Split & { realShare: number; projectedShare: number }) | null;
}

const share = (value: number, max: number) => (Number.isFinite(value) && max > 0 ? Math.min(1, Math.max(0, value / max)) : 0);

/** One bar per decision method in the usual order, then the hindsight ceiling; split real/projected when the year is. */
export function comparisonRows(results: Results, headline: StrategyResult | null): BarRow[] {
  const splitYear = yearSplit(results) !== null;
  const rows: Omit<BarRow, 'share' | 'ofBest' | 'split'>[] = [];
  const splits: (Split | null)[] = [];
  for (const s of results.strategies.toSorted((a, b) => rank(a) - rank(b))) {
    rows.push({
      key: s.strategy,
      label: strategyLabel(s),
      value: s.per_battery_usd_year,
      headline: headline !== null && s.strategy === headline.strategy,
      ceiling: false,
    });
    splits.push(splitYear ? splitOf(s) : null);
  }
  if (results.ceiling) {
    rows.push({
      key: 'ceiling',
      label: results.ceiling.label || 'Best possible (perfect hindsight)',
      value: results.ceiling.per_battery_usd_year,
      headline: false,
      ceiling: true,
    });
    splits.push(splitYear ? splitOf(results.ceiling) : null);
  }
  const max = Math.max(0, ...rows.map((r) => (Number.isFinite(r.value) ? r.value : 0)));
  const best = results.ceiling?.per_battery_usd_year;
  return rows.map((r, i) => {
    const sp = splits[i];
    const whole = share(r.value, max);
    // The real part first; the projected part fills the rest of the bar (a loss on either side draws nothing).
    const realShare = sp ? Math.min(whole, share(sp.real, max)) : whole;
    return {
      ...r,
      share: whole,
      ofBest: !r.ceiling && finite(best) && best > 0 && finite(r.value) ? r.value / best : null,
      split: sp ? { ...sp, realShare, projectedShare: Math.max(0, whole - realShare) } : null,
    };
  });
}

export interface CityRow {
  location: string;
  name: string;
  /** A year, for all of the city's batteries (a fleet total, docs/API.md). */
  usd: number;
  /** Bar length, 0-1 of the biggest city. */
  share: number;
}

/** per_location_usd as rows, biggest first; cities with no number are left out. */
export function cityRows(perLocation: Record<string, number> | null | undefined): CityRow[] {
  const entries = Object.entries(perLocation ?? {}).filter(([, usd]) => typeof usd === 'number' && Number.isFinite(usd));
  const max = Math.max(0, ...entries.map(([, usd]) => usd));
  return entries
    .map(([location, usd]) => ({ location, name: cityName(location), usd, share: share(usd, max) }))
    .toSorted((a, b) => b.usd - a.usd || a.name.localeCompare(b.name));
}

/** The fine print, in plain words: which prices, what is left out, and the rules every method follows. */
export function finePrint(results: Pick<Results, 'year'>, split: YearSplit | null): string[] {
  return [
    split
      ? `Real-time prices through ${dayMonth(split.lastRealDay)}, then the AI's predicted prices. That later part is a projection, and it favors the AI forecast, which trades on its own prediction.`
      : `Gross wholesale value at ${results.year} real-time prices.`,
    'Leaves out retail and delivery charges, grid-support services and battery wear.',
    'The fleet is too small to move prices.',
    'No peeking: each decision uses only prices already published, 15 minutes late.',
    'TimesFM-3’s weights are licensed for non-commercial research only; a production version would use TimesFM 2.5.',
  ];
}
