// Chart colors come from the same CSS tokens as the rest of the page (tokens.css:
// one theme, always dark), read when a chart is built.

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

/** Read the current colors (the fallbacks are the same dark tokens, in case the stylesheet is missing). */
export function readChartTheme(): ChartTheme {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  return {
    axis: v('--chart-axis', '#adb4bc'),
    grid: v('--chart-grid', '#3a3f45'),
    text: v('--text', '#eef0f2'),
    muted: v('--text-muted', '#adb4bc'),
    surface: v('--surface', '#1c1f22'),
    price: v('--chart-price', '#eef0f2'),
    plan: v('--chart-plan', '#adb4bc'),
    delivered: v('--chart-delivered', '#8ab4ff'),
    charge: v('--charge', '#2dd4bf'),
    chargeFill: v('--charge-fill', 'rgba(45,212,191,0.18)'),
    sell: v('--sell', '#fbbf24'),
    sellFill: v('--sell-fill', 'rgba(251,191,36,0.2)'),
    forecast: v('--forecast', '#c4b5fd'),
    forecastFill: v('--forecast-fill', 'rgba(167,139,250,0.24)'),
    fail: v('--fail', '#fca5a5'),
    font: v('--font', 'system-ui, sans-serif'),
  };
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
