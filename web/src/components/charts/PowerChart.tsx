// Power the fleet was asked for (dashed) vs power it delivered (solid), in MW.
// Above zero is selling to the grid, below zero is charging. Controller crashes
// are marked so the audience can watch the gap close.

import { useEffect, useRef } from 'react';
import uPlot from 'uplot';
import { liveStore } from '../../live/liveStore.ts';
import { fmtNumber, fmtPowerFlow, fmtTimeOfDay } from '../../lib/format.ts';
import { buildPowerModel, crashMarkers, type WindowKey } from './model.ts';
import { marketTzDate, observeWidth, pxRatio, readChartTheme, timeAxis } from './theme.ts';
import { emptyXWindow, liveXWindow } from './window.ts';

const HEIGHT = 220;

export function PowerChart({ windowKey }: { windowKey: WindowKey }) {
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
    let model = buildPowerModel(state.history);
    let markers = crashMarkers(state.events);
    let win = liveXWindow(state, windowKey) ?? emptyXWindow();
    tip.hidden = true;
    empty.hidden = state.history.length > 0;

    const draw = (u: uPlot) => {
      const { ctx } = u;
      const { left, top, width, height } = u.bbox;
      ctx.save();
      ctx.beginPath();
      ctx.rect(left, top, width, height);
      ctx.clip();

      // Zero line: above it the fleet sells, below it the fleet charges.
      const y0 = u.valToPos(0, 'y', true);
      ctx.strokeStyle = theme.axis;
      ctx.lineWidth = r;
      ctx.beginPath();
      ctx.moveTo(left, y0);
      ctx.lineTo(left + width, y0);
      ctx.stroke();

      ctx.font = `600 ${11 * r}px ${theme.font}`;
      ctx.textAlign = 'left'; // uPlot leaves it set by the axis labels
      ctx.fillStyle = theme.muted;
      if (y0 - top > 18 * r) {
        ctx.textBaseline = 'top';
        ctx.fillText('▲ Selling to the grid', left + 6 * r, top + 4 * r);
      }
      if (top + height - y0 > 18 * r) {
        ctx.textBaseline = 'bottom';
        ctx.fillText('▼ Charging', left + 6 * r, top + height - 4 * r);
      }

      // Controller crashes.
      ctx.strokeStyle = theme.fail;
      ctx.fillStyle = theme.fail;
      ctx.setLineDash([4 * r, 3 * r]);
      ctx.lineWidth = 1.5 * r;
      ctx.textBaseline = 'top';
      for (const m of markers) {
        const x = u.valToPos(m.x, 'x', true);
        if (x < left || x > left + width) continue;
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, top + height);
        ctx.stroke();
        ctx.fillText(`✕ ${m.label}`, x + 4 * r, top + 20 * r);
      }
      ctx.restore();
    };

    const updateTip = (u: uPlot) => {
      const idx = u.cursor.idx;
      const left = u.cursor.left ?? -1;
      if (idx === null || idx === undefined || left < 0 || idx >= model.data[0].length) {
        tip.hidden = true;
        return;
      }
      const [xs, plan, delivered] = model.data;
      const rows: string[] = [];
      if (plan[idx] !== null) rows.push(`Asked for: ${fmtPowerFlow((plan[idx] as number) * 1000)}`);
      if (delivered[idx] !== null) rows.push(`Delivered: ${fmtPowerFlow((delivered[idx] as number) * 1000)}`);
      if (plan[idx] !== null && delivered[idx] !== null) {
        rows.push(`Gap: ${fmtNumber(Math.abs((plan[idx] as number) - (delivered[idx] as number)), 1)} MW`);
      }
      const title = document.createElement('b');
      title.textContent = fmtTimeOfDay(xs[idx] * 1000);
      tip.replaceChildren(
        title,
        ...rows.map((text) => {
          const d = document.createElement('div');
          d.textContent = text;
          return d;
        }),
      );
      tip.hidden = false;
      const ox = u.over.offsetLeft;
      const oy = u.over.offsetTop;
      let tx = ox + left + 14;
      if (tx + tip.offsetWidth > host.clientWidth) tx = ox + left - tip.offsetWidth - 14;
      tip.style.transform = `translate(${Math.max(0, tx)}px, ${oy + 10}px)`;
    };

    const opts: uPlot.Options = {
      width: host.clientWidth || 480,
      height: HEIGHT,
      padding: [10, 24, 0, 0],
      legend: { show: false },
      tzDate: marketTzDate,
      cursor: {
        sync: { key: 'fleet-live' },
        drag: { x: false, y: false, setScale: false },
        points: { size: 6 },
      },
      scales: {
        x: { time: true, range: () => [win.min, win.max] },
        y: {
          range: (_u, min, max) => {
            const lo = Math.min(0, Number.isFinite(min) ? min : 0);
            const hi = Math.max(0, Number.isFinite(max) ? max : 0);
            const pad = Math.max(10, (hi - lo) * 0.12);
            return [lo - (lo < 0 ? pad : pad * 0.4), hi + (hi > 0 ? pad : pad * 0.4)];
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
          values: (_u, splits) => splits.map((v) => `${fmtNumber(v, 0)} MW`),
        },
      ],
      series: [
        {},
        { label: 'Asked for (plan)', stroke: theme.plan, width: 2, dash: [6, 4], points: { show: false } },
        { label: 'Delivered', stroke: theme.delivered, width: 2, points: { show: false } },
      ],
      hooks: { draw: [draw], setCursor: [updateTip] },
    };

    const u = new uPlot(opts, model.data as unknown as uPlot.AlignedData, host);
    host.setAttribute('aria-hidden', 'true');

    let seen = { history: state.history, forecast: state.forecast, events: state.events, settings: state.settings };
    const unsubscribe = liveStore.subscribe(() => {
      state = liveStore.get();
      if (
        state.history === seen.history &&
        state.forecast === seen.forecast &&
        state.events === seen.events &&
        state.settings === seen.settings
      ) {
        return;
      }
      const dataChanged = state.history !== seen.history;
      seen = { history: state.history, forecast: state.forecast, events: state.events, settings: state.settings };
      if (dataChanged) model = buildPowerModel(state.history);
      markers = crashMarkers(state.events);
      win = liveXWindow(state, windowKey) ?? emptyXWindow();
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
  }, [windowKey]);

  return (
    <div
      className="chart"
      style={{ minHeight: HEIGHT }}
      role="img"
      aria-label="Chart of the power the fleet was asked for (dashed) and the power it delivered (solid), in megawatts. Above zero is selling, below zero is charging. The current values are listed above the chart."
    >
      <div ref={hostRef} />
      <div ref={tipRef} className="chart-tip" />
      <div ref={emptyRef} className="chart-empty">
        Power appears here as soon as the replay starts.
      </div>
    </div>
  );
}
