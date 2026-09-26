// Browser storage can be missing or throw (private windows, blocked site data),
// so every access goes through these wrappers and callers get a safe fallback.

type Kind = 'session' | 'local';

function area(kind: Kind): Storage | null {
  try {
    if (typeof window === 'undefined') return null;
    return kind === 'session' ? window.sessionStorage : window.localStorage;
  } catch {
    return null;
  }
}

export function readStorage(kind: Kind, key: string): string | null {
  try {
    return area(kind)?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

export function writeStorage(kind: Kind, key: string, value: string | null): void {
  try {
    const store = area(kind);
    if (!store) return;
    if (value === null) store.removeItem(key);
    else store.setItem(key, value);
  } catch {
    // Storage is a convenience here; the app keeps working without it.
  }
}
