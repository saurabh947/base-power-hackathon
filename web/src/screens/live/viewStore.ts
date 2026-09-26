// View choices on the Live screen (not run state): the map or the detailed
// charts, and the charts' area and time window. The map/charts choice is kept
// for the tab (sessionStorage), so a reload stays where the visitor was.

import type { WindowKey } from '../../components/charts/model.ts';
import { readStorage, writeStorage } from '../../lib/storage.ts';
import { createStore, useStore } from '../../lib/store.ts';

export type LiveMode = 'map' | 'charts';

export interface LiveView {
  mode: LiveMode;
  /** The price chart's area (a location id). */
  area: string;
  window: WindowKey;
}

const MODE_KEY = 'fleet-sim.live-view';

export const viewStore = createStore<LiveView>({
  mode: readStorage('session', MODE_KEY) === 'charts' ? 'charts' : 'map',
  area: 'LZ_HOUSTON',
  window: '6h',
});

export function useView<S>(selector: (v: LiveView) => S): S {
  return useStore(viewStore, selector);
}

export function setView(patch: Partial<LiveView>): void {
  viewStore.set((v) => ({ ...v, ...patch }));
  if (patch.mode) writeStorage('session', MODE_KEY, patch.mode);
}
