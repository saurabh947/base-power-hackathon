// Which year the Results page shows (the human's "option A"): the year of the run that exists now (running,
// paused, starting or finished), else the year last picked on Start (remembered in this browser), else 2025,
// the pitch year. The choice is pure (pickResultsYear, tested); useResultsYear finds the facts it needs.

import { useEffect, useState } from 'react';
import { api } from '../api/client.ts';
import type { Settings } from '../api/types.ts';
import { localDateOf } from '../lib/format.ts';
import { readStorage, writeStorage } from '../lib/storage.ts';
import { useLive } from '../live/liveStore.ts';
import { useBackend } from './backend.ts';

/** The pitch year: shown when nothing says otherwise. */
export const DEFAULT_RESULTS_YEAR = 2025;

const KEY = 'fleet-sim.year';

/** Remember the year picked on Start, for the Results page. */
export function rememberYear(year: number): void {
  if (Number.isInteger(year)) writeStorage('local', KEY, String(year));
}

/** The year last picked on Start in this browser, if any. */
export function rememberedYear(): number | null {
  const n = Number(readStorage('local', KEY));
  return Number.isInteger(n) && n >= 2000 && n < 3000 ? n : null;
}

/** A run's year: settings.year, else the Central-time year of its start (an older backend). */
export function runYearOf(settings: Pick<Settings, 'year' | 'start_utc'> | null | undefined): number | null {
  if (!settings) return null;
  if (typeof settings.year === 'number' && Number.isInteger(settings.year)) return settings.year;
  const day = typeof settings.start_utc === 'string' ? localDateOf(settings.start_utc) : null;
  return day ? Number(day.slice(0, 4)) : null;
}

export interface YearFacts {
  /** Whether a run exists now; null while that is not known yet. */
  runExists: boolean | null;
  /** That run's year, when known. */
  runYear: number | null;
  /** The year last picked on Start. */
  remembered: number | null;
}

/** The year to show, or null while it cannot be told yet (a run exists but its year has not arrived). */
export function pickResultsYear({ runExists, runYear, remembered }: YearFacts): number | null {
  if (runYear !== null) return runYear;
  if (runExists === null) return null;
  if (runExists) return null;
  return remembered ?? DEFAULT_RESULTS_YEAR;
}

const RUN_STATUSES = new Set(['running', 'paused', 'starting', 'finished']);

/**
 * The year the Results page shows, and why ("run", "start", or "default" when nothing says), or null while it is
 * being found out. The live store knows the run's year when this tab has been on the Live page; otherwise
 * GET /state tells it, once. When the simulator cannot be reached, the remembered year stands.
 */
export function useResultsYear(): { year: number | null; source: 'run' | 'start' | 'default' } {
  const backend = useBackend();
  const liveYear = useLive((s) => (s.runId && s.status && RUN_STATUSES.has(s.status) ? runYearOf(s.settings) : null));
  const [stateYear, setStateYear] = useState<{ year: number | null } | null>(null);
  const status = backend.health?.status ?? null;
  const known = backend.phase === 'ready' || backend.phase === 'unreachable' || backend.phase === 'not_configured';
  const runExists = status !== null ? RUN_STATUSES.has(status) : known ? false : null;

  useEffect(() => {
    if (!runExists || liveYear !== null) return;
    const controller = new AbortController();
    api.state(controller.signal).then(
      (raw) => setStateYear({ year: runYearOf((raw as { settings?: Settings | null } | null)?.settings) }),
      () => {
        if (!controller.signal.aborted) setStateYear({ year: null });
      },
    );
    return () => controller.abort();
  }, [runExists, liveYear]);

  const runYear = liveYear ?? stateYear?.year ?? null;
  const remembered = rememberedYear();
  // A run whose year could not be read (GET /state failed) falls back to the remembered year.
  const year = runExists && runYear === null && stateYear !== null ? (remembered ?? DEFAULT_RESULTS_YEAR) : pickResultsYear({ runExists, runYear, remembered });
  return { year, source: runYear !== null ? 'run' : remembered !== null ? 'start' : 'default' };
}
