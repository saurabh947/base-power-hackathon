import type { LiveState } from '../../live/reducer.ts';
import { computeXWindow, forecastEnd, WINDOW_SECONDS, type WindowKey, type XWindow } from './model.ts';

/** The shared visible range for the live charts, from the current live state. */
export function liveXWindow(state: LiveState, windowKey: WindowKey): XWindow | null {
  const h = state.history;
  const first = h.length ? Date.parse(h[0].ts_utc) / 1000 : null;
  const last = h.length ? Date.parse(h[h.length - 1].ts_utc) / 1000 : null;
  const start = state.settings?.start_utc ? Date.parse(state.settings.start_utc) / 1000 : null;
  const firstS = first ?? (start !== null && Number.isFinite(start) ? start : null);
  return computeXWindow(firstS, last, forecastEnd(state.forecast), WINDOW_SECONDS[windowKey]);
}

/** A fallback range when there is nothing to show yet. */
export function emptyXWindow(): XWindow {
  const now = Math.floor(Date.now() / 1000);
  return { min: now - 6 * 3600, max: now };
}
