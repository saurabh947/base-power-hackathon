// Presenter mode. The passcode lives in sessionStorage only (gone when the tab
// closes) and is sent as the X-Presenter-Passcode header on presenter calls.

import { createStore, useStore } from '../lib/store.ts';
import { readStorage, writeStorage } from '../lib/storage.ts';

const KEY = 'fleet-sim.presenter-passcode';

export const presenterStore = createStore<{ passcode: string | null }>({ passcode: readStorage('session', KEY) });

export function unlockPresenter(passcode: string): void {
  writeStorage('session', KEY, passcode);
  presenterStore.set({ passcode });
}

export function leavePresenter(): void {
  writeStorage('session', KEY, null);
  presenterStore.set({ passcode: null });
}

export function presenterPasscode(): string | null {
  return presenterStore.get().passcode;
}

export function useIsPresenter(): boolean {
  return useStore(presenterStore, (s) => s.passcode !== null);
}
