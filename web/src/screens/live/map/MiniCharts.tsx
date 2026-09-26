// Small line charts for the map's corners: the price of the selected city (with charge/sell shading, the
// forecast band, and the AI's predicted prices dashed) and power asked for vs delivered. They end at the
// moment the timeline shows. Decorative: the values they show are always written out next to them.

import type { Forecast, Tick } from '../../../api/types.ts';
import { useLiveData } from '../../../live/timeTravel.ts';
import { scopeValue, type Scope } from './scope.ts';
import { actionSpans } from './spans.ts';

const W = 280;
const H = 56;
const PAST_S = 6 * 3600;
const MIN_PAST_S = 2 * 3600;
const AHEAD_S = 3 * 3600;
const toS = (iso: string) => Date.parse(iso) / 1000;

interface Frame {
  x0: number;
  x1: number;
  lo: number;
  hi: number;
}

const sx = (f: Frame, x: number) => ((x - f.x0) / (f.x1 - f.x0)) * W;
const sy = (f: Frame, y: number) => H - 2 - ((y - f.lo) / (f.hi - f.lo)) * (H - 6);

function path(f: Frame, pts: [number, number][]): string {
  let d = '';
  let pen = false;
  for (const [x, y] of pts) {
    if (!Number.isFinite(y)) {
      pen = false;
      continue;
    }
    d += `${pen ? 'L' : 'M'}${sx(f, x).toFixed(1)},${sy(f, y).toFixed(1)}`;
    pen = true;
  }
  return d;
}

/** The last 6 hours of ticks; early in a run, from its start (at least 2 hours wide, so it draws into space). */
function visibleTicks(history: Tick[]): { ticks: Tick[]; now: number; from: number } | null {
  if (!history.length) return null;
  const now = toS(history[history.length - 1].ts_utc);
  let i = history.length - 1;
  while (i > 0 && toS(history[i - 1].ts_utc) >= now - PAST_S) i--;
  const from = Math.min(now - MIN_PAST_S, Math.max(now - PAST_S, toS(history[i].ts_utc)));
  return { ticks: history.slice(i), now, from };
}

export function MiniPriceChart({ location }: { location: string }) {
  const history = useLiveData((s) => s.history);
  const forecast: Forecast | undefined = useLiveData((s) => s.forecast[location]);
  const seen = visibleTicks(history);
  if (!seen) return <div className="mini-chart mini-empty">The price line appears when the replay starts.</div>;
  const { ticks, now, from } = seen;
  // Real prices solid; the AI's predicted prices (2026, after the real data ends) dashed. The two share the
  // tick where the prediction begins, so the line is unbroken.
  const real: [number, number][] = ticks.map((t) => [toS(t.ts_utc), t.predicted ? Number.NaN : (t.price?.[location] ?? Number.NaN)]);
  const firstPredicted = ticks.findIndex((t) => t.predicted);
  const predicted: [number, number][] = ticks.map((t, i) => [
    toS(t.ts_utc),
    t.predicted || i === firstPredicted - 1 ? (t.price?.[location] ?? Number.NaN) : Number.NaN,
  ]);
  const band = (forecast?.points ?? [])
    .map((p) => ({ x: toS(p.ts_utc), lo: p.p10, hi: p.p90 }))
    .filter((p) => p.x > now && p.x <= now + AHEAD_S && Number.isFinite(p.lo) && Number.isFinite(p.hi));
  const x0 = from;
  const x1 = now + (band.length ? AHEAD_S : 15 * 60);
  const values = ticks
    .map((t) => t.price?.[location] ?? Number.NaN)
    .concat(band.flatMap((p) => [p.lo, p.hi]))
    .filter(Number.isFinite);
  const lo = Math.min(0, ...values);
  let hi = Math.max(...values, lo + 20);
  hi += (hi - lo) * 0.08;
  const f: Frame = { x0, x1, lo, hi };
  const spans = actionSpans(
    ticks.map((t) => toS(t.ts_utc)),
    ticks.map((t) => t.action?.[location] ?? null),
  );
  const bandPath =
    band.length > 1
      ? `M${band.map((p) => `${sx(f, p.x).toFixed(1)},${sy(f, p.hi).toFixed(1)}`).join('L')}L${band
          .toReversed()
          .map((p) => `${sx(f, p.x).toFixed(1)},${sy(f, p.lo).toFixed(1)}`)
          .join('L')}Z`
      : '';
  const nowX = sx(f, now);
  return (
    <svg className="mini-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
      {spans.map((s) => (
        <g key={`${s.action}-${s.start}`} className={s.action === 'sell' ? 'mini-sell' : 'mini-charge'}>
          <rect x={sx(f, Math.max(s.start, x0))} y={0} width={Math.max(0, sx(f, Math.min(s.end, now + 300)) - sx(f, Math.max(s.start, x0)))} height={H} />
          <rect className="mini-strip" x={sx(f, Math.max(s.start, x0))} y={0} width={Math.max(0, sx(f, Math.min(s.end, now + 300)) - sx(f, Math.max(s.start, x0)))} height={2} />
        </g>
      ))}
      {bandPath ? <path className="mini-band" d={bandPath} /> : null}
      {lo < 0 ? <line className="mini-zero" x1={0} x2={W} y1={sy(f, 0)} y2={sy(f, 0)} /> : null}
      <path className="mini-line" d={path(f, real)} />
      {firstPredicted >= 0 ? <path className="mini-line mini-predicted" d={path(f, predicted)} /> : null}
      <line className="mini-now" x1={nowX} x2={nowX} y1={0} y2={H} />
    </svg>
  );
}

export function MiniPowerChart({ scope }: { scope: Scope }) {
  const history = useLiveData((s) => s.history);
  const seen = visibleTicks(history);
  if (!seen) return <div className="mini-chart mini-empty">Power appears when the replay starts.</div>;
  const { ticks, now, from } = seen;
  const asked: [number, number][] = ticks.map((t) => [toS(t.ts_utc), (scopeValue(t.target_kw, scope) ?? Number.NaN) / 1000]);
  const delivered: [number, number][] = ticks.map((t) => [toS(t.ts_utc), (scopeValue(t.actual_kw, scope) ?? Number.NaN) / 1000]);
  const values = asked.concat(delivered).map((p) => p[1]).filter(Number.isFinite);
  const lo = Math.min(0, ...values);
  const hi = Math.max(0, ...values);
  const pad = Math.max(1, (hi - lo) * 0.12);
  const f: Frame = { x0: from, x1: now + 300, lo: lo - (lo < 0 ? pad : 0), hi: hi + (hi > 0 ? pad : 0) + (hi === lo ? 1 : 0) };
  return (
    <svg className="mini-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
      <line className="mini-zero" x1={0} x2={W} y1={sy(f, 0)} y2={sy(f, 0)} />
      <path className="mini-asked" d={path(f, asked)} />
      <path className="mini-delivered" d={path(f, delivered)} />
    </svg>
  );
}
