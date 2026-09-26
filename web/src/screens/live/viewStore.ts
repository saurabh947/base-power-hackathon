// View choices shared by the Live screen's charts (not run state).

import type { LocationId } from '../../api/types.ts';
import type { WindowKey } from '../../components/charts/model.ts';
import { createStore, useStore } from '../../lib/store.ts';

export interface LiveView {
  area: LocationId;
  window: WindowKey;
}

export const viewStore = createStore<LiveView>({ area: 'LZ_HOUSTON', window: '6h' });

export function useView<S>(selector: (v: LiveView) => S): S {
  return useStore(viewStore, selector);
}

export function setView(patch: Partial<LiveView>): void {
  viewStore.set((v) => ({ ...v, ...patch }));
}
