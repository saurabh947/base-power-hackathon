// Short confirmations and errors after a button press ("Controller 7 crashed").

import { createStore, useStore } from '../lib/store.ts';

export type ToastKind = 'success' | 'info' | 'error';

export interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  detail?: string;
  technical?: string;
}

export const toastStore = createStore<Toast[]>([]);

let nextId = 1;

export function dismissToast(id: number): void {
  toastStore.set((list) => list.filter((t) => t.id !== id));
}

export function showToast(toast: Omit<Toast, 'id'>, timeoutMs = toast.kind === 'error' ? 9000 : 5000): number {
  const id = nextId++;
  toastStore.set((list) => [...list.slice(-3), { ...toast, id }]);
  setTimeout(() => dismissToast(id), timeoutMs);
  return id;
}

export function useToasts(): Toast[] {
  return useStore(toastStore, (s) => s);
}
