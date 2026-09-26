// The race (docs/API.md Race): the live fleet against a shadow copy of itself that runs the comparison
// strategy on the same batteries, prices and availability. Pure, so the wording and the numbers are unit
// tested; Race.tsx draws it on the map's money corner and in the "Show charts" money card.
//
// "AI forecast $46,310 · Simple rule $31,020 · AI forecast ahead by $15,290", for all of Texas or one city
// (race.by_location). Labels come from race.label and the live strategy's label.

import type { Race, Tick, TickAction, Totals } from '../../api/types.ts';
import { fmtPercent, fmtUsd, shortLabel, strategyName } from '../../lib/format.ts';
import type { Scope } from './map/scope.ts';

function finite(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

export interface RaceSide {
  /** "AI forecast", "Simple rule". */
  label: string;
  usd: number;
  /** "$46,310" (cents while both sums are under $100). */
  text: string;
  /** Bar length, 0-1 of the larger sum (0 while a side has earned nothing). */
  share: number;
  /** 0-1 charge level now, if known. */
  charge: number | null;
  /** What the side is doing now in the selected city (null for all of Texas). */
  action: TickAction | null;
}

export type Leader = 'live' | 'race' | 'level';

export interface RaceView {
  live: RaceSide;
  race: RaceSide;
  /** Live minus race, dollars: positive means the live strategy is ahead. */
  diff: number;
  leader: Leader;
  /** "AI forecast ahead by $15,290", "Simple rule ahead by $1,204", "Level so far". */
  lead: string;
  /** "Charge left: AI forecast 54%, simple rule 21%". */
  charge: string | null;
  /** The whole line, for screen readers and tooltips. */
  summary: string;
}

export interface RaceInput {
  race: Race | null;
  /** The live run's settings.strategy. */
  liveStrategy: string | null;
  totals: Totals;
  /** The latest tick, for the live fleet's charge level and actions. */
  tick: Tick | null;
  scope: Scope;
}

/** "Simple rule" → "simple rule", but "AI forecast" stays. */
function lowerFirst(label: string): string {
  return /^[A-Z][a-z]/.test(label) ? label[0].toLowerCase() + label.slice(1) : label;
}

/** "the simple rule", "the AI forecast": how the race's strategy is named in a sentence. */
export function raceName(race: Pick<Race, 'label' | 'strategy'>): string {
  return `the ${lowerFirst(shortLabel(race.label || strategyName(race.strategy)))}`;
}

/** Null while idle (race is null), and for a city the race or the totals have no numbers for. */
export function raceView({ race, liveStrategy, totals, tick, scope }: RaceInput): RaceView | null {
  if (!race || !finite(race.revenue_usd)) return null;
  // docs/API.md: the comparison is bands, or forecast when the live run itself uses bands.
  const liveId = liveStrategy || (race.strategy === 'bands' ? 'forecast' : 'bands');
  const liveLabel = shortLabel(strategyName(liveId));
  const raceLabel = shortLabel(race.label || strategyName(race.strategy));

  let liveUsd: number;
  let raceUsd: number;
  let diff: number;
  let liveCharge: number | null;
  let raceCharge: number | null;
  let liveAction: TickAction | null = null;
  let raceAction: TickAction | null = null;
  if (scope === null) {
    const level = tick?.charge_level?.fleet;
    liveUsd = totals.revenue_usd;
    raceUsd = race.revenue_usd;
    diff = finite(race.live_minus_race_usd) ? race.live_minus_race_usd : liveUsd - raceUsd;
    liveCharge = finite(level) ? level : null;
    raceCharge = finite(race.charge_level) ? race.charge_level : null;
  } else {
    const r = race.by_location?.[scope];
    const l = totals.by_location?.[scope];
    if (!r || !l || !finite(r.revenue_usd) || !finite(l.revenue_usd)) return null;
    const level = tick?.charge_level?.[scope];
    liveUsd = l.revenue_usd;
    raceUsd = r.revenue_usd;
    diff = liveUsd - raceUsd;
    liveCharge = finite(level) ? level : null;
    raceCharge = finite(r.charge_level) ? r.charge_level : null;
    liveAction = tick?.action?.[scope] ?? null;
    raceAction = r.action ?? null;
  }
  if (!finite(liveUsd) || !finite(diff)) return null;

  // Cents while the sums are small (the first minutes of a replay), whole dollars after.
  const decimals = Math.max(Math.abs(liveUsd), Math.abs(raceUsd)) < 100 ? 2 : 0;
  const money = (n: number) => fmtUsd(n, decimals);
  const leader: Leader = Math.abs(diff) < (decimals ? 0.005 : 0.5) ? 'level' : diff > 0 ? 'live' : 'race';
  const lead = leader === 'level' ? 'Level so far' : `${leader === 'live' ? liveLabel : raceLabel} ahead by ${money(Math.abs(diff))}`;
  const max = Math.max(liveUsd, raceUsd, 0);
  const share = (n: number) => (max > 0 ? Math.min(1, Math.max(0, n / max)) : 0);
  const charge =
    liveCharge !== null && raceCharge !== null
      ? `Charge left: ${liveLabel} ${fmtPercent(liveCharge)}, ${lowerFirst(raceLabel)} ${fmtPercent(raceCharge)}`
      : null;

  return {
    live: { label: liveLabel, usd: liveUsd, text: money(liveUsd), share: share(liveUsd), charge: liveCharge, action: liveAction },
    race: { label: raceLabel, usd: raceUsd, text: money(raceUsd), share: share(raceUsd), charge: raceCharge, action: raceAction },
    diff,
    leader,
    lead,
    charge,
    summary: `${liveLabel} ${money(liveUsd)} · ${raceLabel} ${money(raceUsd)} · ${lead}`,
  };
}
