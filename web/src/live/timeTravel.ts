// "Viewing 7:45 pm · Back to live" (docs/UX.md, "Live map v2", the timeline): which moment the map and its
// corners show. null is live; otherwise the sim time (ms) of a tick in the history. The simulation keeps
// running meanwhile. `shownStore` is the live state as of that moment (src/live/past.ts), and the map's
// corners read it through useLiveData; the controls always read the live store.

import { createContext, useContext } from 'react';
import { createStore, useStore, type Store } from '../lib/store.ts';
import { liveStore } from './liveStore.ts';
import { pastState, type FleetCities } from './past.ts';
import { FrameRecorder, type FrameRecord } from './recorder.ts';
import { initialLiveState, type LiveState } from './reducer.ts';

/** The moment being viewed (sim time, ms), or null for live. */
export const viewingStore = createStore<number | null>(null);

/** Everything this page has seen, frame by frame. */
export const recorder = new FrameRecorder();

/** The live state as of the viewed moment (the live state itself while live). */
export const shownStore = createStore<LiveState>(initialLiveState);

export function useViewing(): number | null {
  return useStore(viewingStore, (t) => t);
}

/** Show the map and its corners as they were at `tMs` (null: live). */
export function viewAt(tMs: number | null): void {
  viewingStore.set(tMs);
}

export function backToLive(): void {
  viewingStore.set(null);
}

/** Which store the live-map corners read: the viewed moment's. Everything else reads the live store. */
export const LiveDataContext = createContext<Store<LiveState>>(liveStore);

/** Like useLive, but the viewed moment's state inside the map (LiveDataContext), else the live state. */
export function useLiveData<S>(selector: (state: LiveState) => S): S {
  return useStore(useContext(LiveDataContext), selector);
}

/** Where the batteries are (the map's layout, once it has loaded), to count a controller's batteries per city. */
let fleetCities: () => FleetCities | undefined = () => undefined;

export function provideFleetCities(get: () => FleetCities | undefined): void {
  fleetCities = get;
}

// ---- keeping shownStore in step ---------------------------------------------------------------

interface Cache {
  tMs: number;
  runId: string | null;
  firstTick: string | null;
  record: FrameRecord | null;
  events: LiveState['events'];
  forecast: LiveState['forecast'];
  state: LiveState;
}

let cache: Cache | null = null;

function sync(): void {
  const live = liveStore.get();
  const tMs = viewingStore.get();
  if (tMs === null) {
    cache = null;
    shownStore.set(live);
    return;
  }
  // A new run, a reset, or nothing to look back on: back to live.
  if (!live.history.length || live.status === 'idle' || (cache && cache.runId !== live.runId)) {
    cache = null;
    viewingStore.set(null);
    return;
  }
  const record = recorder.at(tMs);
  const firstTick = live.history[0]?.ts_utc ?? null;
  // The past does not change as the run goes on: keep the same objects, and refresh only the run's own facts.
  if (cache && cache.tMs === tMs && cache.firstTick === firstTick && cache.record === record && cache.events === live.events && cache.forecast === live.forecast) {
    const s = cache.state;
    if (s.status !== live.status || s.speedTps !== live.speedTps || s.settings !== live.settings || s.received !== live.received) {
      cache.state = { ...s, status: live.status, speedTps: live.speedTps, settings: live.settings, received: live.received };
      shownStore.set(cache.state);
    }
    return;
  }
  const state = pastState(live, tMs, record, fleetCities());
  const prev = cache?.state;
  // Keep slices that did not change, so their panels do not re-render.
  if (prev && cache?.tMs === tMs && cache.firstTick === firstTick) {
    if (prev.history.length === state.history.length) state.history = prev.history;
    if (JSON.stringify(prev.totals) === JSON.stringify(state.totals)) state.totals = prev.totals;
    if (prev.events.length === state.events.length && prev.events[0]?.id === state.events[0]?.id) state.events = prev.events;
    if (JSON.stringify(prev.workers) === JSON.stringify(state.workers)) state.workers = prev.workers;
  }
  cache = { tMs, runId: live.runId, firstTick, record, events: live.events, forecast: live.forecast, state };
  shownStore.set(state);
}

let holders = 0;
let unsubscribe: (() => void) | null = null;

/**
 * Record the frames and keep shownStore in step while the Live page is open; the returned function lets go.
 * Leaving the page marks a gap in the recording and goes back to live.
 */
export function holdTimeline(): () => void {
  holders += 1;
  if (!unsubscribe) {
    const onLive = () => {
      recorder.add(liveStore.get());
      sync();
    };
    const offLive = liveStore.subscribe(onLive);
    const offView = viewingStore.subscribe(sync);
    unsubscribe = () => {
      offLive();
      offView();
    };
    recorder.add(liveStore.get());
    sync();
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    holders -= 1;
    if (holders > 0) return;
    unsubscribe?.();
    unsubscribe = null;
    recorder.gap();
    viewingStore.set(null);
    cache = null;
    shownStore.set(liveStore.get());
  };
}
