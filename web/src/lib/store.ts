// A minimal external store. Components read slices through useStore(selector),
// so a live frame every second re-renders only the panels whose slice changed,
// never the whole app. The map's canvas goes further and subscribes imperatively.

import { useSyncExternalStore } from 'react';

export interface Store<T> {
  get(): T;
  set(next: T | ((prev: T) => T)): void;
  subscribe(listener: () => void): () => void;
}

export function createStore<T>(initial: T): Store<T> {
  let state = initial;
  const listeners = new Set<() => void>();
  return {
    get: () => state,
    set(next) {
      const value = typeof next === 'function' ? (next as (prev: T) => T)(state) : next;
      if (Object.is(value, state)) return;
      state = value;
      // Copy first: a listener may unsubscribe while we iterate.
      for (const listener of Array.from(listeners)) listener();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

/**
 * Read one slice of a store. The selector must return something already held
 * in the state (a field or a primitive), not a freshly built object, so React
 * can tell when it really changed.
 */
export function useStore<T, S>(store: Store<T>, selector: (state: T) => S): S {
  const read = () => selector(store.get());
  return useSyncExternalStore(store.subscribe, read, read);
}
