// The price line for one area, shaded teal where batteries charged and amber
// where they sold, with the AI forecast as a soft 10–90% band. Updates
// imperatively from the live store: React does not re-render it per frame.

import { useEffect, useRef } from 'react';
import uPlot from 'uplot';
import { liveStore } from '../../live/liveStore.ts';
import { actionLabel, areaName, fmtCount, fmtPrice, fmtPriceMwh, fmtTimeOfDay } from '../../lib/format.ts';
import { actionSpans, buildPriceModel, type WindowKey } from './model.ts';
import { marketTzDate, observeWidth, pxRatio, readChartTheme, timeAxis } from './theme.ts';
import { emptyXWindow, liveXWindow } from './window.ts';

const HEIGHT = 250;

interface Props {
  area: string;
  windowKey: WindowKey;
  technical: boolean;
}

function setTipLines(tip: HTMLElement, lines: [string, string][]) {
  tip.replaceChildren(
    ...lines.map(([label, value], i) => {
      const row = document.createElement('div');
      if (i === 0) {
        const b = document.createElement('b');
        b.textContent = label;
        row.append(b);
      } else {
        row.textContent = value ? `${label}: ${value}` : label;
      }
      return row;
    }),
  );
}

export function PriceChart({ area, windowKey, technical }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const emptyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    const tip = tipRef.current;
    const empty = emptyRef.current;
    if (!host || !tip || !empty) return;
    const theme = readChartTheme();
    const r = pxRatio();

    let state = liveStore.get();
    let model = buildPriceModel(state.history, state.forecast[area], area);
    let spans = actionSpans(model.data[0], model.actions);
    let win = liveXWindow(state, windowKey) ?? emptyXWindow();
    let lastTickX = state.history.length ? Date.parse(state.history[state.history.length - 1].ts_utc) / 1000 : null;
    tip.hidden = true;
    empty.hidden = state.history.length > 0;

    const drawShading = (u: uPlot) => {
      const { ctx } = u;
      const { left, top, width, height } = u.bbox;
      ctx.save();
      ctx.beginPath();
      ctx.rect(left, top, width, height);
      ctx.clip();
      ctx.font = `600 ${11 * r}px ${theme.font}`;
      ctx.textAlign = 'left'; // uPlot leaves it set by the axis labels
      ctx.textBaseline = 'top';
      for (const s of spans) {
        const x0 = u.valToPos(s.start, 'x', true);
        const x1 = u.valToPos(s.end, 'x', true);
        if (x1 < left || x0 > left + width) continue;
        const charge = s.action === 'charge';
        ctx.fillStyle = charge ? theme.chargeFill : theme.sellFill;
        ctx.fillRect(x0, top, x1 - x0, height);
        ctx.fillStyle = charge ? theme.charge : theme.sell;
        ctx.fillRect(x0, top, x1 - x0, 3 * r);
        const visible0 = Math.max(x0, left);
        if (Math.min(x1, left + width) - visible0 > 70 * r) {
          ctx.fillText(charge ? '▼ Charging' : '▲ Selling', visible0 + 5 * r, top + 7 * r);
        }
      }
      ctx.restore();
    };

    const drawNow = (u: uPlot) => {
      if (lastTickX === null) return;
      const { ctx } = u;
      const { top, height, left, width } = u.bbox;
      const x = u.valToPos(lastTickX, 'x', true);
      if (x < left || x > left + width) return;
      ctx.save();
      ctx.strokeStyle = theme.muted;
      ctx.lineWidth = r;
      ctx.setLineDash([3 * r, 3 * r]);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, top + height);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = theme.muted;
      ctx.font = `600 ${11 * r}px ${theme.font}`;
      ctx.textAlign = 'left'; // uPlot leaves it set by the axis labels
      ctx.textBaseline = 'bottom';
      ctx.fillText('now', x + 4 * r, top + height - 4 * r);
      ctx.restore();
    };

    const updateTip = (u: uPlot) => {
      const idx = u.cursor.idx;
      const left = u.cursor.left ?? -1;
      if (idx === null || idx === undefined || left < 0) {
        tip.hidden = true;
        return;
      }
      const [xs, price, p90, p10] = model.data;
      const lines: [string, string][] = [[fmtTimeOfDay(xs[idx] * 1000), '']];
      if (price[idx] !== null) lines.push(['Price', fmtPrice(price[idx] as number)]);
      const action = model.actions[idx];
      if (action) lines.push([`Batteries: ${actionLabel(action).toLowerCase()}`, '']);
      if (p10[idx] !== null && p90[idx] !== null) {
        lines.push([
          'AI forecast range',
          `${fmtPriceMwh(p10[idx] as number)} to ${fmtPriceMwh(p90[idx] as number)}`,
        ]);
      }
      setTipLines(tip, lines);
      tip.hidden = false;
      const ox = u.over.offsetLeft;
      const oy = u.over.offsetTop;
      let tx = ox + left + 14;
      if (tx + tip.offsetWidth > host.clientWidth) tx = ox + left - tip.offsetWidth - 14;
      tip.style.transform = `translate(${Math.max(0, tx)}px, ${oy + 10}px)`;
    };

    const opts: uPlot.Options = {
      width: host.clientWidth || 640,
      height: HEIGHT,
      padding: [10, 24, 0, 0],
      legend: { show: false },
      tzDate: marketTzDate,
      cursor: {
        sync: { key: 'fleet-live' },
        drag: { x: false, y: false, setScale: false },
        points: { size: 7 },
      },
      scales: {
        x: { time: true, range: () => [win.min, win.max] },
        y: {
          range: (_u, min, max) => {
            const lo = Math.min(0, Number.isFinite(min) ? min : 0);
            const hi = Number.isFinite(max) && max > lo ? max : lo + 50;
            return [lo < 0 ? lo * 1.1 : 0, hi * 1.1];
          },
        },
      },
      axes: [
        timeAxis(theme),
        {
          stroke: theme.axis,
          font: `12px ${theme.font}`,
          grid: { stroke: theme.grid, width: 1 },
          ticks: { stroke: theme.grid, width: 1, size: 4 },
          size: 64,
          values: (_u, splits) => splits.map((v) => `$${fmtCount(v)}`),
        },
      ],
      series: [
        {},
        { label: 'Price ($/MWh)', stroke: theme.price, width: 2, points: { show: false } },
        { label: 'Forecast high (p90)', stroke: theme.forecast, width: 0.75, spanGaps: true, points: { show: false } },
        { label: 'Forecast low (p10)', stroke: theme.forecast, width: 0.75, spanGaps: true, points: { show: false } },
        {
          label: 'Forecast middle (p50)',
          stroke: theme.forecast,
          width: 1.25,
          dash: [5, 4],
          spanGaps: true,
          show: technical,
          points: { show: false },
        },
        {
          label: 'Price the batteries acted on (15 min late)',
          stroke: theme.muted,
          width: 1,
          dash: [2, 3],
          show: technical,
          points: { show: false },
        },
      ],
      bands: [{ series: [2, 3], fill: theme.forecastFill }],
      hooks: { drawClear: [drawShading], draw: [drawNow], setCursor: [updateTip] },
    };

    const u = new uPlot(opts, model.data as unknown as uPlot.AlignedData, host);
    host.setAttribute('aria-hidden', 'true');

    let seen = { history: state.history, forecast: state.forecast, settings: state.settings };
    const unsubscribe = liveStore.subscribe(() => {
      state = liveStore.get();
      if (state.history === seen.history && state.forecast === seen.forecast && state.settings === seen.settings) return;
      seen = { history: state.history, forecast: state.forecast, settings: state.settings };
      model = buildPriceModel(state.history, state.forecast[area], area);
      spans = actionSpans(model.data[0], model.actions);
      win = liveXWindow(state, windowKey) ?? emptyXWindow();
      lastTickX = state.history.length ? Date.parse(state.history[state.history.length - 1].ts_utc) / 1000 : null;
      empty.hidden = state.history.length > 0;
      u.setData(model.data as unknown as uPlot.AlignedData);
    });
    const stopResize = observeWidth(host, (width) => u.setSize({ width, height: HEIGHT }));
    const hideTip = () => {
      tip.hidden = true;
    };
    host.addEventListener('mouseleave', hideTip);

    return () => {
      unsubscribe();
      stopResize();
      host.removeEventListener('mouseleave', hideTip);
      u.destroy();
    };
  }, [area, windowKey, technical]);

  return (
    <div
      className="chart"
      style={{ minHeight: HEIGHT }}
      role="img"
      aria-label={`Chart of the ${areaName(area)} electricity price over the replay, shaded where batteries were charging or selling. The current values are listed above the chart.`}
    >
      <div ref={hostRef} />
      <div ref={tipRef} className="chart-tip" />
      <div ref={emptyRef} className="chart-empty">
        Prices appear here as soon as the replay starts.
      </div>
    </div>
  );
}
