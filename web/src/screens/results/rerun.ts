// "Re-run the full year" and its Stop button (docs/API.md: POST /backtest, GET /backtest/{job_id},
// POST /backtest/{job_id}/cancel). Pure, so which button is enabled and what a stop leads to are unit tested;
// RerunPanel.tsx does the polling and the drawing.

import { ApiError } from '../../api/client.ts';
import { friendlyError, type FriendlyError } from '../../api/errors.ts';

/** Nothing running; a re-run running; a Stop sent and not answered yet. */
export type RerunPhase = 'idle' | 'running' | 'stopping';

export function rerunPhase(job: unknown, stopping: boolean): RerunPhase {
  if (!job) return 'idle';
  return stopping ? 'stopping' : 'running';
}

/** The run button only while nothing runs; Stop only while a re-run is running (and not already stopping). */
export function rerunButtons(phase: RerunPhase): { run: boolean; stop: boolean } {
  return { run: phase === 'idle', stop: phase === 'running' };
}

export type StopOutcome =
  /** 200 {"status": "cancelled"}: stopped, nothing stored, the previous results stay. */
  | { kind: 'stopped' }
  /** 409 not_running: it had just finished (or been stopped); its status, polled next, says how. */
  | { kind: 'not_running' }
  /** 404: the simulator no longer knows the job (it restarted); forget it. */
  | { kind: 'unknown' }
  /** Anything else, e.g. the simulator could not be reached; the re-run goes on. */
  | { kind: 'error'; error: FriendlyError };

/** POST /backtest/{job_id}/cancel, turned into what the panel does next. */
export async function stopRerun(jobId: string, cancel: (jobId: string) => Promise<unknown>): Promise<StopOutcome> {
  try {
    await cancel(jobId);
    return { kind: 'stopped' };
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) return { kind: 'not_running' };
    if (err instanceof ApiError && err.status === 404) return { kind: 'unknown' };
    return { kind: 'error', error: friendlyError(err, 'backtest') };
  }
}

/** Said once a re-run has stopped, by this Stop or by someone else's (the fleet, and its re-run, are shared). */
export function stoppedMessage(hasResults: boolean): string {
  return hasResults ? 'Stopped. The previous results are still shown.' : 'Stopped. No full-year results were saved.';
}
