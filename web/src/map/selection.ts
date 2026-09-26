// Which city the live map is showing: null means all of Texas. Selecting a city
// switches the corners to its numbers and highlights the controllers serving it.

import { createStore, useStore } from '../lib/store.ts';

export type SelectionAction =
  /** A city label (button) was pressed: select it, or go back to all of Texas if it already was. */
  | { type: 'label'; location: string }
  /** The map was clicked: on a city's dots (`location`) or on empty map (null). */
  | { type: 'map'; location: string | null }
  /** The "All of Texas" chip. */
  | { type: 'all' }
  /** The Escape key. */
  | { type: 'escape' };

/** The next selection. Locations not in `known` are ignored (a stale label, a city the layout dropped). */
export function nextSelection(current: string | null, action: SelectionAction, known: readonly string[]): string | null {
  switch (action.type) {
    case 'label':
      if (!known.includes(action.location)) return current;
      return current === action.location ? null : action.location;
    case 'map':
      if (action.location === null) return null;
      return known.includes(action.location) ? action.location : current;
    case 'all':
    case 'escape':
      return null;
    default:
      return current;
  }
}

export const selectionStore = createStore<string | null>(null);

export function useSelectedCity(): string | null {
  return useStore(selectionStore, (s) => s);
}

export function dispatchSelection(action: SelectionAction, known: readonly string[]): void {
  selectionStore.set(nextSelection(selectionStore.get(), action, known));
}

/** Escape clears the selection, unless something else is using the key (a dialog, a menu, a text field). */
export function escapeClears(e: { key: string; defaultPrevented: boolean; target: EventTarget | null }): boolean {
  if (e.key !== 'Escape' || e.defaultPrevented) return false;
  const el = e.target as { tagName?: string; isContentEditable?: boolean; closest?: (s: string) => unknown } | null;
  const tag = el?.tagName?.toLowerCase();
  if (tag === 'input' || tag === 'select' || tag === 'textarea' || el?.isContentEditable) return false;
  if (el?.closest?.('dialog[open]')) return false;
  return true;
}
