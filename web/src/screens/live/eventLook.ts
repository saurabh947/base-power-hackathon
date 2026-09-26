// How a "What just happened" item looks, on the map and in "Show charts": its icon, its colour class and
// what a screen reader hears first. Race updates (docs/API.md race_update) get a flag of their own, and the
// moment the AI's prediction takes over (prediction_started) the AI's sparkle in the forecast's violet.

import type { LiveEvent } from '../../api/types.ts';
import type { IconName } from '../../components/Icon.tsx';

const SEVERITY_ICON: Record<string, IconName> = { good: 'check', warn: 'alert', info: 'info' };
const SPOKEN: Record<string, string> = { good: 'Good news: ', warn: 'Warning: ' };

type Look = Pick<LiveEvent, 'kind' | 'severity'>;

const isRace = (e: Look) => e.kind === 'race_update';
const isPrediction = (e: Look) => e.kind === 'prediction_started';

/** A flag for the race, a sparkle for the prediction; otherwise the severity's check, alert or info sign. */
export function eventIcon(e: Look): IconName {
  if (isRace(e)) return 'flag';
  if (isPrediction(e)) return 'ai';
  return SEVERITY_ICON[e.severity] ?? 'info';
}

/** "feed-good", "feed-info feed-race", …: the colour follows the severity, as for every event. */
export function eventClass(e: Look): string {
  if (isPrediction(e)) return 'feed-predict';
  return `feed-${e.severity in SEVERITY_ICON ? e.severity : 'info'}${isRace(e) ? ' feed-race' : ''}`;
}

/** Said before the message, since the icon is decorative. */
export function eventSpoken(e: Look): string {
  if (isRace(e)) return 'Race update: ';
  if (isPrediction(e)) return 'Prediction: ';
  return SPOKEN[e.severity] ?? '';
}
