// Crashing a controller, open to everyone (docs/UX.md, Crashing controllers): the call, the shared countdown
// (one crash every 2 s for everyone together, docs/API.md), whether a crash can be asked for now, and the
// state of the "Crash controller 7?" confirmation on a controller tile. The buttons and the popover that use
// these are in ./controls.tsx.

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../../api/client.ts';
import { friendlyError } from '../../api/errors.ts';
import type { KillResponse, Worker } from '../../api/types.ts';
import { useLive } from '../../live/liveStore.ts';
import { cityName } from '../../lib/cities.ts';
import { controllerName } from '../../lib/fleet.ts';
import { fmtList } from '../../lib/format.ts';
import { createStore, useStore } from '../../lib/store.ts';
import { showToast } from '../../state/toasts.ts';

/** docs/API.md: one crash per 2 s, for everyone together. */
export const CRASH_SPACING_S = 2;

interface CrashState {
  busy: boolean;
  /** No crash from this page before this time (ms): after our own crash, or a 429's retry_after_s. */
  untilMs: number;
  /** When `untilMs` was set, so the first render of a countdown needs no clock of its own. */
  setAtMs: number;
}

export const crashStore = createStore<CrashState>({ busy: false, untilMs: 0, setAtMs: 0 });

/** "Houston", "San Antonio and Austin": where a controller's batteries are. */
export function workerCities(worker: Worker | undefined): string | null {
  const locs = Object.entries(worker?.by_location ?? {})
    .filter(([, n]) => n > 0)
    .map(([loc]) => cityName(loc));
  return locs.length ? fmtList(locs) : null;
}

/**
 * Crash a controller (a random working one when `workerId` is left out), with a toast either way. A 429 or
 * our own crash starts the shared countdown, so the buttons say when the next crash is allowed.
 */
export async function crashController(workerId?: number): Promise<KillResponse | null> {
  if (crashStore.get().busy) return null;
  crashStore.set((s) => ({ ...s, busy: true }));
  try {
    const result = await api.kill(workerId);
    const now = Date.now();
    crashStore.set({ busy: false, untilMs: now + CRASH_SPACING_S * 1000, setAtMs: now });
    showToast({
      kind: 'success',
      title: `${controllerName(result.worker_id)} crashed.`,
      detail: 'Its batteries pulse red on the map until the other controllers take them over, usually within seconds.',
      technical: `worker_id ${result.worker_id}, pid ${result.pid}, killed at ${result.killed_at_utc}`,
    });
    return result;
  } catch (err) {
    const f = friendlyError(err, 'kill');
    const now = Date.now();
    crashStore.set({ busy: false, untilMs: f.retryAfterS ? now + f.retryAfterS * 1000 : 0, setAtMs: now });
    showToast({ kind: 'error', title: f.title, detail: f.detail, technical: f.technical });
    return null;
  }
}

/** Whole seconds until the next crash is allowed from this page (0 = now), and whether one is in flight. */
export function useCrashWait(): { busy: boolean; waitS: number } {
  const { busy, untilMs, setAtMs } = useStore(crashStore, (s) => s);
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

/** Can a crash be asked for right now? And if not, why (for a tooltip). */
export function useCrashable(): { can: boolean; why: string | null } {
  const status = useLive((s) => s.status);
  const workers = useLive((s) => s.workers);
  const healthy = workers.filter((w) => w.status === 'healthy').length;
  if (status !== 'running' && status !== 'paused') return { can: false, why: 'Start a simulation first.' };
  if (workers.length && healthy <= 2) {
    return { can: false, why: 'At least 2 controllers must keep working. Press Reset to bring the others back.' };
  }
  return { can: true, why: null };
}

/** The "Crash controller 7?" popover: which controller, and where it sits over the tiles. */
export interface CrashTarget {
  workerId: number;
  /** Position in the tiles' wrapper (.mini-tiles-wrap or .tiles-wrap, positioned): above the tile. */
  left: number;
  bottom: number;
  button: HTMLElement;
}

export const CRASH_POP_WIDTH = 232;

/**
 * Crash-by-tile: `ask(workerId, button)` from a tile's click opens the confirmation above that tile (a second
 * click on the same tile closes it); `close(true)` puts the focus back on the tile.
 */
export function useCrashAsk(): {
  target: CrashTarget | null;
  ask: (workerId: number, button: HTMLElement) => void;
  close: (refocus: boolean) => void;
} {
  const [target, setTarget] = useState<CrashTarget | null>(null);
  const current = useRef<CrashTarget | null>(null);
  useEffect(() => {
    current.current = target;
  }, [target]);
  const ask = useCallback((workerId: number, button: HTMLElement) => {
    if (current.current?.workerId === workerId) {
      setTarget(null);
      return;
    }
    const box = button.closest<HTMLElement>('.mini-tiles-wrap, .tiles-wrap');
    if (!box) return;
    const b = button.getBoundingClientRect();
    const c = box.getBoundingClientRect();
    const center = b.left - c.left + b.width / 2;
    const left = Math.max(0, Math.min(c.width - CRASH_POP_WIDTH, center - CRASH_POP_WIDTH / 2));
    setTarget({ workerId, left, bottom: c.bottom - b.top + 6, button });
  }, []);
  const close = useCallback((refocus: boolean) => {
    const t = current.current;
    setTarget(null);
    if (refocus && t?.button.isConnected) t.button.focus();
  }, []);
  return { target, ask, close };
}
