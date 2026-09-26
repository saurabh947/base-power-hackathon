// Is the simulator reachable, and is a run going? The backend scales to zero on
// Cloud Run, so the first request can take a while: after a second without an
// answer (or on network errors and 503s) the phase becomes "waking" and the UI
// says "Waking up the simulator… (usually a few seconds)" while this retries.

import { api } from '../api/client.ts';
import { API_BASE } from '../api/config.ts';
import { friendlyError, type FriendlyError } from '../api/errors.ts';
import type { HealthResponse } from '../api/types.ts';
import { createStore, useStore } from '../lib/store.ts';

export type BackendPhase = 'unknown' | 'checking' | 'waking' | 'ready' | 'unreachable' | 'not_configured';

export interface BackendState {
  phase: BackendPhase;
  health: HealthResponse | null;
  error: FriendlyError | null;
  checkedAtMs: number | null;
}

export const backendStore = createStore<BackendState>({
  phase: API_BASE ? 'unknown' : 'not_configured',
  health: null,
  error: null,
  checkedAtMs: null,
});

/** How long to keep retrying a sleeping backend before saying it is unreachable. */
const WAKE_BUDGET_MS = 90_000;
/** Show "Waking up…" when the first answer takes longer than this. */
const SLOW_MS = 1_000;

let inFlight: Promise<void> | null = null;

function patch(p: Partial<BackendState>) {
  backendStore.set((s) => ({ ...s, ...p }));
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Check GET /health (at most one check at a time). Resolves when ready or given up. */
export function checkBackend(): Promise<void> {
  if (!API_BASE) return Promise.resolve();
  if (inFlight) return inFlight;
  inFlight = (async () => {
    const started = Date.now();
    const current = backendStore.get();
    if (current.phase !== 'ready') patch({ phase: 'checking', error: null });
    const slow = setTimeout(() => {
      if (backendStore.get().phase === 'checking') patch({ phase: 'waking' });
    }, SLOW_MS);
    try {
      for (let attempt = 1; ; attempt++) {
        try {
          const health = await api.health();
          patch({ phase: 'ready', health, error: null, checkedAtMs: Date.now() });
          return;
        } catch (err) {
          const friendly = friendlyError(err, 'load');
          if (!friendly.unreachable || Date.now() - started > WAKE_BUDGET_MS) {
            patch({ phase: 'unreachable', error: friendly, checkedAtMs: Date.now() });
            return;
          }
          patch({ phase: 'waking', error: friendly });
          await sleep(Math.min(5_000, 1_000 * attempt));
        }
      }
    } finally {
      clearTimeout(slow);
      inFlight = null;
    }
  })();
  return inFlight;
}

export function useBackend(): BackendState {
  return useStore(backendStore, (s) => s);
}

/** The mock API identifies itself with a version starting with "mock". */
export function isPracticeServer(health: HealthResponse | null): boolean {
  return !!health && typeof health.version === 'string' && health.version.toLowerCase().startsWith('mock');
}
