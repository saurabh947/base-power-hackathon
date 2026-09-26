// The global "Show technical details" switch. Off by default; remembered per
// browser as a convenience (localStorage), never needed for the site to work.

import { createStore, useStore } from '../lib/store.ts';
import { readStorage, writeStorage } from '../lib/storage.ts';

const KEY = 'fleet-sim.show-technical';

export const prefsStore = createStore({ technical: readStorage('local', KEY) === '1' });

export function setTechnical(on: boolean): void {
  prefsStore.set((s) => ({ ...s, technical: on }));
  writeStorage('local', KEY, on ? '1' : '0');
}

export function useTechnical(): boolean {
  return useStore(prefsStore, (s) => s.technical);
}
