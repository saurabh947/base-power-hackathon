// Data for the live map: the fleet layout (GET /fleet/layout, fetched once and
// kept by version) and, for every snapshot or frame, the decoded battery_status
// with counts per city. Decoded once here and shared by the canvas and the
// corner panels. Runs while a map is on screen (holdMapFeed).
//
// While the timeline shows an earlier moment (src/live/timeTravel.ts), the codes
// are the battery_status this page recorded for that moment; before the page
// was opened there is none, and each city's dots show that city's action then
// (city totals only).

import { api } from '../api/client.ts';
import { API_BASE } from '../api/config.ts';
import type { LiveEvent, Tick } from '../api/types.ts';
import { setCities } from '../lib/cities.ts';
import { createStore, useStore } from '../lib/store.ts';
import { liveStore } from '../live/liveStore.ts';
import { indexAt } from '../live/past.ts';
import type { FrameRecord } from '../live/recorder.ts';
import type { LiveState } from '../live/reducer.ts';
import { provideFleetCities, recorder, viewingStore } from '../live/timeTravel.ts';
import { ControllerFlash } from './flash.ts';
import { readLayout, type MapLayout } from './layout.ts';
import { countCodes, decodePacked, decodeStatus, synthesizeStatus, WAITING, type CodeCounts } from './status.ts';

export interface LayoutState {
  layout: MapLayout | null;
  phase: 'idle' | 'loading' | 'ready' | 'error';
  /** Size of the positions field, for technical details. */
  positionsChars: number;
  loadMs: number | null;
}

export const layoutStore = createStore<LayoutState>({ layout: null, phase: 'idle', positionsChars: 0, loadMs: null });

// The timeline rebuilds past controllers per city from where the batteries are.
provideFleetCities(() => {
  const l = layoutStore.get().layout;
  return l ? { size: l.size, cityOf: l.cityOf, locations: l.cities.map((c) => c.location) } : undefined;
});

export function useLayoutState(): LayoutState {
  return useStore(layoutStore, (s) => s);
}

/**
 * battery_status from the server (live) or as recorded by this page (an earlier moment); each city's action
 * when there is none (an older backend, or a moment before the page was opened); or nothing yet.
 */
export type StatusSource = 'battery_status' | 'recorded' | 'from_actions' | 'city_level' | 'none';

export interface StatusView {
  /** A code for every battery (src/map/status.ts), or null before the layout arrives. */
  codes: Uint8Array | null;
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
/** What the last update showed: live, or an earlier moment (with the record and layout it used). */
let shown: { tMs: number; record: FrameRecord | null; layout: MapLayout | null } | null = null;

function refreshStatus(force = false): void {
  const viewing = viewingStore.get();
  if (viewing !== null) {
    refreshPast(viewing, force);
    return;
  }
  // Back from an earlier moment: decode the live status afresh.
  refreshLive(force || shown !== null);
  shown = null;
}

function emptyView(statusChars: number): StatusView {
  return { codes: null, source: 'none', fleet: EMPTY_COUNTS, byCity: [], statusChars, decodeMs: 0, fromEvents: 0 };
}

/** An earlier moment: the battery_status recorded then, or each city's action then (before the page was opened). */
function refreshPast(tMs: number, force: boolean): void {
  const layout = layoutStore.get().layout;
  const record = recorder.at(tMs);
  if (!force && shown && shown.tMs === tMs && shown.record === record && shown.layout === layout) return;
  shown = { tMs, record, layout };
  if (!layout) {
    statusStore.set(emptyView(0));
    return;
  }
  const t0 = performance.now();
  const state = liveStore.get();
  let codes: Uint8Array | null = record?.status ? decodePacked(record.status, layout.size) : null;
  let source: StatusSource = codes ? 'recorded' : 'city_level';
  if (!codes) {
    const tick = state.history[Math.max(0, indexAt(state.history, tMs))] ?? null;
    codes = synthesizeStatus(layout.size, layout.cityOf, layout.cities.map((c) => c.location), tick);
    if (!tick) source = 'none';
  }
  statusStore.set({
    codes,
    source,
    fleet: countCodes(codes, null, null),
    byCity: layout.cities.map((_, k) => countCodes(codes, layout.cityOf, k)),
    statusChars: record?.status ? record.status.length : 0,
    decodeMs: performance.now() - t0,
    fromEvents: 0,
  });
}

function refreshLive(force = false): void {
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
    statusStore.set(emptyView(state.batteryStatus?.length ?? 0));
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
    const offLive = liveStore.subscribe(() => refreshStatus());
    const offViewing = viewingStore.subscribe(() => refreshStatus());
    unsubscribe = () => {
      offLive();
      offViewing();
    };
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
