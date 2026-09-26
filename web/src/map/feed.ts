// Data for the live map: the fleet layout (GET /fleet/layout, fetched once and
// kept by version) and, for every snapshot or frame, the decoded battery_status
// with counts per city. Decoded once here and shared by the canvas and the
// corner panels. Runs while a map is on screen (holdMapFeed).

import { api } from '../api/client.ts';
import { API_BASE } from '../api/config.ts';
import type { LiveEvent, Tick } from '../api/types.ts';
import { setCities } from '../lib/cities.ts';
import { createStore, useStore } from '../lib/store.ts';
import { liveStore } from '../live/liveStore.ts';
import type { LiveState } from '../live/reducer.ts';
import { ControllerFlash } from './flash.ts';
import { readLayout, type MapLayout } from './layout.ts';
import { countCodes, decodeStatus, synthesizeStatus, WAITING, type CodeCounts } from './status.ts';

export interface LayoutState {
  layout: MapLayout | null;
  phase: 'idle' | 'loading' | 'ready' | 'error';
  /** Size of the positions field, for technical details. */
  positionsChars: number;
  loadMs: number | null;
}

export const layoutStore = createStore<LayoutState>({ layout: null, phase: 'idle', positionsChars: 0, loadMs: null });

export function useLayoutState(): LayoutState {
  return useStore(layoutStore, (s) => s);
}

export type StatusSource = 'battery_status' | 'from_actions' | 'none';

export interface StatusView {
  /** A code for every battery (src/map/status.ts), or null before the layout arrives. */
  codes: Uint8Array | null;
  /** battery_status from the server; a stand-in from each city's action when it is missing; or nothing yet. */
  source: StatusSource;
  fleet: CodeCounts;
  /** Indexed like layout.cities. */
  byCity: CodeCounts[];
  /** Length of the last battery_status field (base64 characters). */
  statusChars: number;
  decodeMs: number;
  /** Batteries shown as "controller down" from the worker events because the frame missed the code. */
  fromEvents: number;
}

const EMPTY_COUNTS: CodeCounts = [0, 0, 0, 0, 0];
export const statusStore = createStore<StatusView>({
  codes: null,
  source: 'none',
  fleet: EMPTY_COUNTS,
  byCity: [],
  statusChars: 0,
  decodeMs: 0,
  fromEvents: 0,
});

export function useMapStatus<S>(selector: (s: StatusView) => S): S {
  return useStore(statusStore, selector);
}

// ---- the layout ------------------------------------------------------------------

let retryTimer: ReturnType<typeof setTimeout> | null = null;
let lastLoadStartMs = 0;

async function loadLayout(): Promise<void> {
  if (!API_BASE) return;
  const phase = layoutStore.get().phase;
  if (phase === 'loading') return;
  lastLoadStartMs = Date.now();
  layoutStore.set((s) => ({ ...s, phase: 'loading' }));
  const t0 = performance.now();
  try {
    const raw = await api.layout();
    const layout = readLayout(raw);
    if (!layout) throw new Error('unreadable layout');
    const positionsChars = typeof (raw as { positions?: unknown }).positions === 'string' ? (raw as { positions: string }).positions.length : 0;
    const prev = layoutStore.get().layout;
    layoutStore.set({
      layout: prev && prev.version === layout.version && prev.size === layout.size ? prev : layout,
      phase: 'ready',
      positionsChars,
      loadMs: performance.now() - t0,
    });
    if (layout.cities.length) setCities(layout.cities);
  } catch {
    layoutStore.set((s) => ({ ...s, phase: 'error' }));
    if (holders > 0 && retryTimer === null) {
      retryTimer = setTimeout(() => {
        retryTimer = null;
        if (holders > 0) void loadLayout();
      }, 10_000);
    }
  }
  refreshStatus(true);
}

/** The fleet changed size under us (a new layout on the server): fetch it again, at most every 30 s. */
function maybeReload(state: LiveState, layout: MapLayout): void {
  const size = state.settings?.fleet_size;
  if (typeof size === 'number' && size !== layout.size && Date.now() - lastLoadStartMs > 30_000) void loadLayout();
}

// ---- the status ------------------------------------------------------------------

interface Seen {
  status: string | null;
  tick: Tick | null;
  layout: MapLayout | null;
  events: LiveEvent[] | null;
  /** The last update marked batteries down from events: decode the next one afresh so they come back. */
  synthetic: boolean;
}

let seen: Seen = { status: null, tick: null, layout: null, events: null, synthetic: false };
const flash = new ControllerFlash();
let flashKey = '';

function refreshStatus(force = false): void {
  const state = liveStore.get();
  const layout = layoutStore.get().layout;
  const tick = state.history.length ? state.history[state.history.length - 1] : null;
  if (
    !force &&
    !seen.synthetic &&
    seen.status === state.batteryStatus &&
    seen.layout === layout &&
    seen.events === state.events &&
    (state.batteryStatus !== null || seen.tick === tick)
  ) {
    return;
  }
  seen = { status: state.batteryStatus, tick, layout, events: state.events, synthetic: false };
  if (!layout) {
    statusStore.set({
      codes: null,
      source: 'none',
      fleet: EMPTY_COUNTS,
      byCity: [],
      statusChars: state.batteryStatus?.length ?? 0,
      decodeMs: 0,
      fromEvents: 0,
    });
    return;
  }
  maybeReload(state, layout);
  const t0 = performance.now();
  let codes: Uint8Array | null = state.batteryStatus ? decodeStatus(state.batteryStatus, layout.size) : null;
  let source: StatusSource = codes ? 'battery_status' : 'none';
  if (!codes && tick && state.status !== 'idle') {
    codes = synthesizeStatus(layout.size, layout.cityOf, layout.cities.map((c) => c.location), tick);
    source = 'from_actions';
  }
  if (!codes) codes = new Uint8Array(layout.size).fill(WAITING);

  // Controller ownership starts over with every run (and every reset, and every new layout).
  const key = `${layout.version}|${layout.size}|${state.runId ?? 'no run'}`;
  if (key !== flashKey) {
    flash.reset(layout.size, state.workers.length || 20, state.events);
    flashKey = key;
  }
  const fromEvents = state.status === 'idle' ? 0 : flash.apply(state.events, codes, Date.now());
  if (fromEvents) seen.synthetic = true;

  const byCity = layout.cities.map((_, k) => countCodes(codes, layout.cityOf, k));
  const fleet = countCodes(codes, null, null);
  statusStore.set({
    codes,
    source,
    fleet,
    byCity,
    statusChars: state.batteryStatus?.length ?? 0,
    decodeMs: performance.now() - t0,
    fromEvents,
  });
}

// ---- lifetime ----------------------------------------------------------------------

let holders = 0;
let unsubscribe: (() => void) | null = null;

/** Keep the map's data flowing while the returned release function has not been called. */
export function holdMapFeed(): () => void {
  holders += 1;
  if (!unsubscribe) {
    unsubscribe = liveStore.subscribe(() => refreshStatus());
    refreshStatus(true);
  }
  const phase = layoutStore.get().phase;
  if (phase === 'idle' || phase === 'error') void loadLayout();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    holders -= 1;
    if (holders > 0) return;
    unsubscribe?.();
    unsubscribe = null;
    if (retryTimer !== null) clearTimeout(retryTimer);
    retryTimer = null;
  };
}
