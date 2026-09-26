// Runs of charging or selling for the corners' mini price line (MiniCharts.tsx), which shades them blue and
// green. Pure, so it is unit tested.

import type { TickAction } from '../../../api/types.ts';

/** A tick is 5 minutes. */
const TICK_SECONDS = 300;

export interface ActionSpan {
  start: number;
  end: number;
  action: 'charge' | 'sell';
}

/** Runs of charging or selling; each tick covers [ts, ts + 5 min). Holds and gaps in the data split runs. */
export function actionSpans(xs: number[], actions: (TickAction | null)[]): ActionSpan[] {
  const spans: ActionSpan[] = [];
  let current: ActionSpan | null = null;
  for (let k = 0; k < xs.length; k++) {
    const a = actions[k];
    if (a === null || a === undefined) continue; // no action known for this point
    if (a === 'charge' || a === 'sell') {
      if (current && current.action === a && xs[k] <= current.end) {
        current.end = xs[k] + TICK_SECONDS;
      } else {
        current = { start: xs[k], end: xs[k] + TICK_SECONDS, action: a };
        spans.push(current);
      }
    } else {
      current = null;
    }
  }
  return spans;
}
