// Which city the live map is showing: null means all of Texas. Selecting a city
// switches the corners to its numbers; it also becomes the price chart's area in
// the detailed layout ("Show charts").

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

let onSelect: ((location: string | null) => void) | null = null;

/** Called with every new selection (the Live screen uses it to keep the price chart's area in step). */
export function onSelectionChange(listener: ((location: string | null) => void) | null): void {
  onSelect = listener;
}

export function dispatchSelection(action: SelectionAction, known: readonly string[]): void {
  const prev = selectionStore.get();
  const next = nextSelection(prev, action, known);
  if (next === prev) return;
  selectionStore.set(next);
  onSelect?.(next);
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
