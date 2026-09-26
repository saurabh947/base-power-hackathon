// How a "What just happened" item looks, on the map and in "Show charts": its icon, its colour class and
// what a screen reader hears first. Race updates (docs/API.md race_update) get a flag of their own.

import type { LiveEvent } from '../../api/types.ts';
import type { IconName } from '../../components/Icon.tsx';

const SEVERITY_ICON: Record<string, IconName> = { good: 'check', warn: 'alert', info: 'info' };
const SPOKEN: Record<string, string> = { good: 'Good news: ', warn: 'Warning: ' };

type Look = Pick<LiveEvent, 'kind' | 'severity'>;

const isRace = (e: Look) => e.kind === 'race_update';

/** A flag for the race; otherwise the severity's check, alert or info sign. */
export function eventIcon(e: Look): IconName {
  return isRace(e) ? 'flag' : (SEVERITY_ICON[e.severity] ?? 'info');
}

/** "feed-good", "feed-info feed-race", …: the colour follows the severity, as for every event. */
export function eventClass(e: Look): string {
  return `feed-${e.severity in SEVERITY_ICON ? e.severity : 'info'}${isRace(e) ? ' feed-race' : ''}`;
}

/** Said before the message, since the icon is decorative. */
export function eventSpoken(e: Look): string {
  return isRace(e) ? 'Race update: ' : (SPOKEN[e.severity] ?? '');
}
