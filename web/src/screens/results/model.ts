// What the Results panels draw, from GET /results/pitch (docs/API.md). Pure, so
// the order of the rows and the lengths of the bars are unit tested.

import type { Results, StrategyResult } from '../../api/types.ts';
import { cityName } from '../../lib/cities.ts';
import { strategyName } from '../../lib/format.ts';

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
  /** The method of the big number (green). */
  headline: boolean;
  /** "Best possible (perfect hindsight)" (hatched). */
  ceiling: boolean;
}

const share = (value: number, max: number) => (Number.isFinite(value) && max > 0 ? Math.min(1, Math.max(0, value / max)) : 0);

/** One bar per decision method in the usual order, then the hindsight ceiling. */
export function comparisonRows(results: Results, headline: StrategyResult | null): BarRow[] {
  const rows: Omit<BarRow, 'share'>[] = results.strategies
    .toSorted((a, b) => rank(a) - rank(b))
    .map((s) => ({
      key: s.strategy,
      label: strategyLabel(s),
      value: s.per_battery_usd_year,
      headline: headline !== null && s.strategy === headline.strategy,
      ceiling: false,
    }));
  if (results.ceiling) {
    rows.push({
      key: 'ceiling',
      label: results.ceiling.label || 'Best possible (perfect hindsight)',
      value: results.ceiling.per_battery_usd_year,
      headline: false,
      ceiling: true,
    });
  }
  const max = Math.max(0, ...rows.map((r) => (Number.isFinite(r.value) ? r.value : 0)));
  return rows.map((r) => ({ ...r, share: share(r.value, max) }));
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
