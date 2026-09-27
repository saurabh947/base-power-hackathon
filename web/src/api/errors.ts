// Turns any failure into a short, plain-language message. The server's own
// `message` (docs/API.md promises one plain sentence) is preferred when it has
// one; rate limits, outages and missing features get our wording.

import { ApiError } from './client.ts';

export type ErrorContext = 'load' | 'start' | 'control' | 'availability' | 'kill' | 'restart' | 'results';

export interface FriendlyError {
  title: string;
  detail: string;
  /** Seconds before trying again makes sense (429), when the server said so. */
  retryAfterS: number | null;
  /** Raw status and code, shown only with "Show technical details". */
  technical: string;
  /** The request never reached a working server (network, timeout, 503). */
  unreachable: boolean;
}

function seconds(n: number): string {
  const s = Math.max(1, Math.ceil(n));
  return s === 1 ? '1 second' : `${s} seconds`;
}

export function friendlyError(err: unknown, context: ErrorContext = 'load'): FriendlyError {
  if (!(err instanceof ApiError)) {
    return {
      title: 'Something went wrong.',
      detail: 'Please try again. If it keeps happening, reload the page.',
      retryAfterS: null,
      technical: err instanceof Error ? `${err.name}: ${err.message}` : String(err),
      unreachable: false,
    };
  }

  const technical = `HTTP ${err.status || 'n/a'} · ${err.code}${err.serverMessage ? ` · ${err.serverMessage}` : ''}`;
  const base = { retryAfterS: err.retryAfterS, technical, unreachable: false };
  const server = err.serverMessage;

  if (err.code === 'not_configured') {
    return {
      ...base,
      title: 'This site is not connected to a simulator.',
      detail: 'The site was built without VITE_API_BASE_URL, so it does not know where the simulator runs.',
    };
  }
  if (err.status === 0) {
    return {
      ...base,
      unreachable: true,
      title: err.code === 'timeout' ? 'The simulator is taking a long time to answer.' : "Can't reach the simulator.",
      detail: 'It may be waking up, or your connection dropped. Try again in a moment.',
    };
  }

  switch (err.status) {
    case 429: {
      const wait = err.retryAfterS != null ? `Try again in ${seconds(err.retryAfterS)}.` : 'Try again in a few seconds.';
      if (context === 'kill') {
        // docs/API.md: one crash per 2 s, for everyone together.
        return {
          ...base,
          title: 'One crash every 2 seconds, please.',
          detail: `Everyone shares one fleet, so crashes are spaced out. ${wait}`,
        };
      }
      if (context === 'restart') {
        // docs/API.md: restarts are rate-limited like crashes.
        return {
          ...base,
          title: 'One restart every 2 seconds, please.',
          detail: `Everyone shares one fleet, so restarts are spaced out. ${wait}`,
        };
      }
      return {
        ...base,
        title: 'Too many requests right now.',
        detail: `Everyone shares one fleet, so buttons are rate-limited. ${wait}`,
      };
    }
    case 501:
      return {
        ...base,
        title: "This part isn't switched on yet.",
        detail: server ?? 'The simulator does not support this yet. Everything else still works.',
      };
    case 503:
      if (err.code === 'simulator_restarting') {
        const wait = err.retryAfterS != null ? `Try again in ${seconds(err.retryAfterS)}.` : 'Try again in a few seconds.';
        return {
          ...base,
          title: 'The simulator is still restarting after a reset.',
          detail: `Its 20 controllers are coming back up. ${wait}`,
        };
      }
      return {
        ...base,
        unreachable: true,
        title: 'The simulator is busy or still starting.',
        detail: server ?? 'Try again in a few seconds.',
      };
    case 409:
      // docs/API.md POST /chaos/kill and POST /workers/{id}/restart: our own words, which say what to do next.
      if (err.code === 'already_dead') {
        return {
          ...base,
          title: 'That controller has already crashed.',
          detail: 'Pick one that is still working. Crashed controllers stay down until someone restarts them (click one) or presses Reset.',
        };
      }
      if (err.code === 'not_crashed') {
        // docs/API.md POST /workers/{worker_id}/restart: only a crashed controller can be restarted.
        return {
          ...base,
          title: 'That controller is not down.',
          detail: 'Only a crashed controller can be restarted. It may have just been restarted by someone else.',
        };
      }
      if (err.code === 'too_few_healthy') {
        return {
          ...base,
          title: 'At least 2 controllers must keep working.',
          detail: 'Press Reset to bring the crashed controllers back, then try again.',
        };
      }
      return { ...base, title: "That can't happen right now.", detail: server ?? 'The simulation changed. Try again.' };
    case 404:
      if ((context === 'kill' || context === 'restart') && err.code === 'unknown_worker') {
        return { ...base, title: 'There is no such controller.', detail: 'Controllers are numbered 1 to 20.' };
      }
      if (context === 'results' || err.code === 'no_results') {
        // The server's sentence is the same as this title (docs/API.md), so say what to do instead.
        return { ...base, title: 'No full-year results yet.', detail: 'Press "Re-run the full year" to calculate them.' };
      }
      return { ...base, title: 'Not found.', detail: server ?? 'The simulator does not know about that.' };
    case 400:
    case 422:
      return { ...base, title: "The simulator didn't accept that.", detail: server ?? 'Check the values and try again.' };
    default:
      return {
        ...base,
        unreachable: err.status >= 500,
        title: err.status >= 500 ? 'Something went wrong on the simulator.' : 'That request did not work.',
        detail: server ?? 'Please try again in a moment.',
      };
  }
}
