// Crashing and restarting controllers, open to everyone (docs/UX.md, Crashing controllers; docs/API.md, the
// failure model): the calls, their shared countdowns (one crash every 2 s for everyone together, restarts
// likewise), whether a crash can be asked for now, and which gauge's "Crash controller 7?" or "Restart
// controller 7?" popover is open. Nothing comes back on its own: a crashed controller stays down until someone
// restarts it or presses Reset. The buttons and the popover that use these are in ./controls.tsx.

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../../api/client.ts';
import { friendlyError } from '../../api/errors.ts';
import type { KillResponse, RestartResponse, Worker } from '../../api/types.ts';
import { useLive } from '../../live/liveStore.ts';
import { cityName } from '../../lib/cities.ts';
import { controllerName } from '../../lib/fleet.ts';
import { fmtList } from '../../lib/format.ts';
import { createStore, useStore, type Store } from '../../lib/store.ts';
import { showToast } from '../../state/toasts.ts';

/** docs/API.md: one crash per 2 s, for everyone together; restarts are rate-limited the same way. */
export const CRASH_SPACING_S = 2;

interface SpacedState {
  busy: boolean;
  /** Not again from this page before this time (ms): after our own call, or a 429's retry_after_s. */
  untilMs: number;
  /** When `untilMs` was set, so the first render of a countdown needs no clock of its own. */
  setAtMs: number;
}

const idle: SpacedState = { busy: false, untilMs: 0, setAtMs: 0 };
export const crashStore = createStore<SpacedState>(idle);
export const restartStore = createStore<SpacedState>(idle);

/** "Houston", "San Antonio and Austin": where a controller's batteries are. */
export function workerCities(worker: Worker | undefined): string | null {
  const locs = Object.entries(worker?.by_location ?? {})
    .filter(([, n]) => n > 0)
    .map(([loc]) => cityName(loc));
  return locs.length ? fmtList(locs) : null;
}

/** Run a rate-limited call with a toast either way; a 429 or our own success starts the shared countdown. */
async function spaced<T>(store: Store<SpacedState>, call: () => Promise<T>, context: 'kill' | 'restart', done: (result: T) => void): Promise<T | null> {
  if (store.get().busy) return null;
  store.set((s) => ({ ...s, busy: true }));
  try {
    const result = await call();
    const now = Date.now();
    store.set({ busy: false, untilMs: now + CRASH_SPACING_S * 1000, setAtMs: now });
    done(result);
    return result;
  } catch (err) {
    const f = friendlyError(err, context);
    const now = Date.now();
    store.set({ busy: false, untilMs: f.retryAfterS ? now + f.retryAfterS * 1000 : 0, setAtMs: now });
    showToast({ kind: 'error', title: f.title, detail: f.detail, technical: f.technical });
    return null;
  }
}

/** Crash a controller (a random working one when `workerId` is left out). */
export function crashController(workerId?: number): Promise<KillResponse | null> {
  return spaced(crashStore, () => api.kill(workerId), 'kill', (result) =>
    showToast({
      kind: 'success',
      title: `${controllerName(result.worker_id)} crashed.`,
      detail:
        'Its batteries lost their charge and pulse red on the map until the other controllers take them over. It stays down until someone restarts it.',
      technical: `worker_id ${result.worker_id}, pid ${result.pid}, killed at ${result.killed_at_utc}`,
    }),
  );
}

/** Bring a crashed controller back: it takes its batteries back with whatever charge they have by then. */
export function restartController(workerId: number): Promise<RestartResponse | null> {
  return spaced(restartStore, () => api.restart(workerId), 'restart', (result) =>
    showToast({
      kind: 'success',
      title: `${controllerName(result.worker_id)} is restarting.`,
      detail: 'In a moment it takes its batteries back, with whatever charge they have by then.',
      technical: `worker_id ${result.worker_id}, status ${result.status}`,
    }),
  );
}

function useWait(store: Store<SpacedState>): { busy: boolean; waitS: number } {
  const { busy, untilMs, setAtMs } = useStore(store, (s) => s);
  // The clock when this mounted; a countdown that starts later counts from its own start (setAtMs).
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (untilMs <= setAtMs) return;
    const timer = setInterval(() => {
      const t = Date.now();
      setNow(t);
      if (t >= untilMs) clearInterval(timer);
    }, 250);
    return () => clearInterval(timer);
  }, [untilMs, setAtMs]);
  return { busy, waitS: Math.max(0, Math.ceil((untilMs - Math.max(now, setAtMs)) / 1000)) };
}

/** Whole seconds until the next crash is allowed from this page (0 = now), and whether one is in flight. */
export function useCrashWait(): { busy: boolean; waitS: number } {
  return useWait(crashStore);
}

/** The same for restarts. */
export function useRestartWait(): { busy: boolean; waitS: number } {
  return useWait(restartStore);
}

/** Can a crash be asked for right now? And if not, why (for a tooltip). */
export function useCrashable(): { can: boolean; why: string | null } {
  const status = useLive((s) => s.status);
  const workers = useLive((s) => s.workers);
  const healthy = workers.filter((w) => w.status === 'healthy').length;
  if (status !== 'running' && status !== 'paused') return { can: false, why: 'Start a simulation first.' };
  if (workers.length && healthy <= 2) {
    return { can: false, why: 'At least 2 controllers must keep working. Restart a crashed one, or press Reset.' };
  }
  return { can: true, why: null };
}

/** The "Crash controller 7?" or "Restart controller 7?" popover: which controller, and where it sits over the gauges. */
export interface AskTarget {
  workerId: number;
  /** Position in the gauges' wrapper (.gauges-wrap, positioned): above the gauge. */
  left: number;
  bottom: number;
  button: HTMLElement;
}

export const ASK_POP_WIDTH = 232;

/**
 * Crash or restart by gauge: `ask(workerId, button)` from a gauge's click opens the popover above that gauge (a
 * second click on the same gauge closes it); `close(true)` puts the focus back on the gauge.
 */
export function useControllerAsk(): {
  target: AskTarget | null;
  ask: (workerId: number, button: HTMLElement) => void;
  close: (refocus: boolean) => void;
} {
  const [target, setTarget] = useState<AskTarget | null>(null);
  const current = useRef<AskTarget | null>(null);
  useEffect(() => {
    current.current = target;
  }, [target]);
  const ask = useCallback((workerId: number, button: HTMLElement) => {
    if (current.current?.workerId === workerId) {
      setTarget(null);
      return;
    }
    const box = button.closest<HTMLElement>('.gauges-wrap');
    if (!box) return;
    const b = button.getBoundingClientRect();
    const c = box.getBoundingClientRect();
    const center = b.left - c.left + b.width / 2;
    const left = Math.max(0, Math.min(c.width - ASK_POP_WIDTH, center - ASK_POP_WIDTH / 2));
    setTarget({ workerId, left, bottom: c.bottom - b.top + 6, button });
  }, []);
  const close = useCallback((refocus: boolean) => {
    const t = current.current;
    setTarget(null);
    if (refocus && t?.button.isConnected) t.button.focus();
  }, []);
  return { target, ask, close };
}
