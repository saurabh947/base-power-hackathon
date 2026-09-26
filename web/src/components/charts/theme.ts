// Chart colors come from the same CSS tokens as the rest of the page, so the
// canvas charts follow light and dark mode. Charts are rebuilt when the
// color scheme changes (useColorScheme).

import { useSyncExternalStore } from 'react';
import uPlot from 'uplot';
import { fmtDate, fmtTimeOfDay, MARKET_TZ } from '../../lib/format.ts';

export interface ChartTheme {
  axis: string;
  grid: string;
  text: string;
  muted: string;
  surface: string;
  price: string;
  plan: string;
  delivered: string;
  charge: string;
  chargeFill: string;
  sell: string;
  sellFill: string;
  forecast: string;
  forecastFill: string;
  fail: string;
  font: string;
}

/** Read the current colors. `scheme` names the color scheme they belong to (charts rebuild when it changes). */
export function readChartTheme(_scheme: 'light' | 'dark'): ChartTheme {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  return {
    axis: v('--chart-axis', '#475569'),
    grid: v('--chart-grid', '#e3e8ef'),
    text: v('--text', '#0f172a'),
    muted: v('--text-muted', '#475569'),
    surface: v('--surface', '#ffffff'),
    price: v('--chart-price', '#1e293b'),
    plan: v('--chart-plan', '#475569'),
    delivered: v('--chart-delivered', '#1d4ed8'),
    charge: v('--charge', '#0f766e'),
    chargeFill: v('--charge-fill', 'rgba(20,184,166,0.16)'),
    sell: v('--sell', '#b45309'),
    sellFill: v('--sell-fill', 'rgba(245,158,11,0.22)'),
    forecast: v('--forecast', '#6d28d9'),
    forecastFill: v('--forecast-fill', 'rgba(124,58,237,0.14)'),
    fail: v('--fail', '#b91c1c'),
    font: v('--font', 'system-ui, sans-serif'),
  };
}

const darkQuery = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

function subscribeScheme(callback: () => void): () => void {
  darkQuery?.addEventListener('change', callback);
  return () => darkQuery?.removeEventListener('change', callback);
}

export function useColorScheme(): 'light' | 'dark' {
  return useSyncExternalStore(
    subscribeScheme,
    () => (darkQuery?.matches ? 'dark' : 'light'),
    () => 'light',
  );
}

/** Tick marks on market-time (Houston) hours; labels like "7 pm", and the date at midnight. */
export function timeAxis(theme: ChartTheme): uPlot.Axis {
  return {
    stroke: theme.axis,
    font: `12px ${theme.font}`,
    grid: { stroke: theme.grid, width: 1 },
    ticks: { stroke: theme.grid, width: 1, size: 4 },
    space: 64,
    values: (_u, splits) =>
      splits.map((s) => {
        const label = fmtTimeOfDay(s * 1000, { compact: true });
        return label === '12 am' ? fmtDate(s * 1000).replace(/, \d{4}$/, '') : label;
      }),
  };
}

export function marketTzDate(ts: number): Date {
  return uPlot.tzDate(new Date(ts * 1000), MARKET_TZ);
}

export function pxRatio(): number {
  return typeof window === 'undefined' ? 1 : window.devicePixelRatio || 1;
}

/** Keep a chart the width of its container. */
export function observeWidth(el: HTMLElement, onWidth: (width: number) => void): () => void {
  let last = 0;
  const ro = new ResizeObserver((entries) => {
    const width = Math.floor(entries[0]?.contentRect.width ?? 0);
    if (width > 0 && width !== last) {
      last = width;
      onWidth(width);
    }
  });
  ro.observe(el);
  return () => ro.disconnect();
}
