// The shared live view. One LiveConnection feeds the reducer; panels read
// slices with useLive(selector). The connection opens while a screen needs it
// and closes 30 s after the last one lets go, so quick navigation doesn't churn.

import { api } from '../api/client.ts';
import type { Settings } from '../api/types.ts';
import { API_BASE, toWebSocketUrl } from '../api/config.ts';
import { createStore, useStore } from '../lib/store.ts';
import { asSnapshot, initialConnectionState, LiveConnection, type ConnectionState } from './connection.ts';
import { initialLiveState, liveReducer, type LiveState } from './reducer.ts';

export const liveStore = createStore<LiveState>(initialLiveState);
export const connectionStore = createStore<ConnectionState>(initialConnectionState);

export function useLive<S>(selector: (state: LiveState) => S): S {
  return useStore(liveStore, selector);
}

export function useConnection<S>(selector: (state: ConnectionState) => S): S {
  return useStore(connectionStore, selector);
}

/**
 * A run was just started from this page: forget the previous run right away, so
 * the Live screen never flashes old data before the first frame of the new one.
 */
export function beginRun(runId: string, settings: Settings): void {
  liveStore.set((s) => ({
    ...initialLiveState,
    received: true,
    runId,
    status: 'starting',
    settings,
    speedTps: settings.speed_tps,
    workers: s.workers,
    availability: s.availability,
    messages: s.messages,
    lastMessageAtMs: s.lastMessageAtMs,
    runChanged: s.runChanged,
  }));
}

export function dispatchLive(message: unknown): void {
  const prev = liveStore.get();
  const next = liveReducer(prev, message);
  liveStore.set(next);
  if (next.runChanged !== prev.runChanged) void resync();
}

let resyncing = false;

/** Fetch a full snapshot (GET /state), e.g. after someone else starts a new run. */
export async function resync(): Promise<void> {
  if (resyncing || !API_BASE) return;
  resyncing = true;
  try {
    dispatchLive(asSnapshot(await api.state()));
  } catch {
    // The live feed keeps running; the next frame or reconnect snapshot catches up.
  } finally {
    resyncing = false;
  }
}

let connection: LiveConnection | null = null;
let holders = 0;
let stopTimer: ReturnType<typeof setTimeout> | null = null;

function onOnline() {
  connection?.retryNow();
}

function onVisible() {
  if (document.visibilityState === 'visible') connection?.retryNow();
}

/** Keep the live feed open while the returned release function has not been called. */
export function holdLiveFeed(): () => void {
  holders += 1;
  if (stopTimer !== null) {
    clearTimeout(stopTimer);
    stopTimer = null;
  }
  if (!connection && API_BASE) {
    connection = new LiveConnection({
      url: toWebSocketUrl(API_BASE),
      fetchState: (signal) => api.state(signal),
      onMessage: dispatchLive,
      onState: (state) => connectionStore.set(state),
    });
    connection.start();
    window.addEventListener('online', onOnline);
    document.addEventListener('visibilitychange', onVisible);
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    holders -= 1;
    if (holders > 0) return;
    stopTimer = setTimeout(() => {
      stopTimer = null;
      if (holders > 0 || !connection) return;
      connection.stop();
      connection = null;
      window.removeEventListener('online', onOnline);
      document.removeEventListener('visibilitychange', onVisible);
    }, 30_000);
  };
}

/** "Try again" from the UI. */
export function reconnectNow(): void {
  connection?.retryNow();
  void resync();
}
