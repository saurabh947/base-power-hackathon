// Turns any failure into a short, plain-language message. The server's own
// `message` (docs/API.md promises one plain sentence) is preferred when it has
// one; rate limits, outages and missing features get our wording.

import { ApiError } from './client.ts';

export type ErrorContext =
  | 'load'
  | 'start'
  | 'control'
  | 'availability'
  | 'battery'
  | 'kill'
  | 'presenter'
  | 'backtest'
  | 'results';

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
      if (context === 'presenter') {
        return { ...base, title: 'Too many wrong passcodes.', detail: `Wait a minute before trying again. ${wait}` };
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
      if (err.code === 'presenter_passcode_not_set') {
        return {
          ...base,
          title: "Presenter mode isn't set up on the server yet.",
          detail: server ?? 'An administrator needs to set the presenter passcode first.',
        };
      }
      return {
        ...base,
        unreachable: true,
        title: 'The simulator is busy or still starting.',
        detail: server ?? 'Try again in a few seconds.',
      };
    case 403:
      return {
        ...base,
        title: "That passcode wasn't accepted.",
        detail:
          context === 'kill'
            ? 'Unlock presenter mode again from the "Presenter" link at the bottom of the page.'
            : (server ?? 'Check the passcode and try again.'),
      };
    case 409:
      if (err.code === 'already_dead') {
        return { ...base, title: 'That controller has already crashed.', detail: server ?? 'Pick a different controller.' };
      }
      if (err.code === 'too_few_healthy') {
        return {
          ...base,
          title: 'At least 2 controllers must keep working.',
          detail: server ?? 'Press Reset to bring the crashed controllers back.',
        };
      }
      return { ...base, title: "That can't happen right now.", detail: server ?? 'The simulation changed. Try again.' };
    case 404:
      if (context === 'battery') {
        return { ...base, title: 'No battery has that ID.', detail: server ?? 'Battery IDs run from b00000 to b19999.' };
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
