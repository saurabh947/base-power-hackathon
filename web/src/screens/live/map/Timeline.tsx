// The timeline scrubber in the map's top-left corner (docs/UX.md, "Live map v2"): a compact track whose
// line is the price over the run so far, green where the fleet sold and blue where it bought, ▲/▼ at each
// day's high and low, and the AI's prediction shaded. Dragging it, or ← / →, shows the map and every corner
// as they were at that moment; the simulation keeps running. The track model is in ./timeline.ts.

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import { useLive } from '../../../live/liveStore.ts';
import { indexAt, stepFrom, tickMs } from '../../../live/past.ts';
import { backToLive, useViewing, viewAt } from '../../../live/timeTravel.ts';
import { useCities } from '../../../lib/cities.ts';
import { fmtPriceMwh, fmtTimeOfDay } from '../../../lib/format.ts';
import type { Scope } from './scope.ts';
import { buildTrack, indexToX, momentLabel, shortDate, xToIndex } from './timeline.ts';

const HEIGHT = 30;
/** Shift+arrow and Page Up/Down move an hour. */
const HOUR_TICKS = 12;


export function Timeline({ scope }: { scope: Scope }) {
  const history = useLive((s) => s.history);
  const viewing = useViewing();
  const cities = useCities();
  const boxRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const dragging = useRef(false);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const measure = () => setWidth(Math.floor(el.clientWidth));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const weights = useMemo(() => Object.fromEntries(cities.map((c) => [c.location, c.batteries])), [cities]);
  const track = useMemo(() => buildTrack(history, scope, weights, width, HEIGHT), [history, scope, weights, width]);

  const n = history.length;
  const last = n - 1;
  const index = viewing === null ? last : Math.max(0, indexAt(history, viewing));
  const firstMs = n ? tickMs(history[0]) : null;
  const shownMs = n ? tickMs(history[Math.max(0, index)]) : null;

  const goTo = (i: number) => {
    if (!n) return;
    if (i >= last) backToLive();
    else viewAt(tickMs(history[Math.max(0, i)]));
  };

  const fromPointer = (e: PointerEvent<HTMLDivElement>) => {
    const el = boxRef.current;
    if (!el || !track) return;
    const rect = el.getBoundingClientRect();
    goTo(xToIndex(track, history, e.clientX - rect.left));
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!n) return;
    const big = e.shiftKey ? HOUR_TICKS : 1;
    let next: number | null | undefined;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') next = stepFrom(history, viewing, -big);
    else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') next = stepFrom(history, viewing, big);
    else if (e.key === 'PageDown') next = stepFrom(history, viewing, -HOUR_TICKS);
    else if (e.key === 'PageUp') next = stepFrom(history, viewing, HOUR_TICKS);
    else if (e.key === 'Home') next = tickMs(history[0]);
    else if (e.key === 'End') next = null;
    else return;
    e.preventDefault();
    e.stopPropagation(); // the map's own arrow keys do the same; once is enough
    viewAt(next);
  };

  const thumbX = track ? indexToX(track, history, index) : 0;
  const predictedX = track?.predictedX ?? null;
  const live = viewing === null;
  const valueText = shownMs === null ? 'No replay yet' : `${live ? 'Live, ' : 'Viewing '}${momentLabel(shownMs, firstMs)}`;

  return (
    <div className={`timeline${live ? ' is-live' : ' is-past'}`}>
      <div
        ref={boxRef}
        className="timeline-track"
        role="slider"
        tabIndex={n ? 0 : -1}
        aria-label="Timeline: drag or use the arrow keys to look back"
        aria-valuemin={0}
        aria-valuemax={Math.max(0, last)}
        aria-valuenow={Math.max(0, index)}
        aria-valuetext={valueText}
        aria-disabled={!n || undefined}
        onKeyDown={onKeyDown}
        onPointerDown={(e) => {
          if (!n || e.button !== 0) return;
          dragging.current = true;
          e.currentTarget.setPointerCapture(e.pointerId);
          fromPointer(e);
        }}
        onPointerMove={(e) => {
          if (dragging.current) fromPointer(e);
        }}
        onPointerUp={(e) => {
          dragging.current = false;
          if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
        }}
        onPointerCancel={() => {
          dragging.current = false;
        }}
      >
        {track ? (
          <svg width={track.width} height={HEIGHT} aria-hidden="true" focusable="false">
            <defs>
              <pattern id="timeline-hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                <rect width="5" height="5" className="timeline-predicted-bg" />
                <line x1="0" y1="0" x2="0" y2="5" className="timeline-predicted-line" />
              </pattern>
            </defs>
            {predictedX !== null ? (
              <rect className="timeline-predicted" x={predictedX} y={0} width={Math.max(0, track.width - predictedX)} height={HEIGHT} />
            ) : null}
            {track.bands.map((b) => (
              <rect key={`${b.kind}-${b.x0.toFixed(1)}`} className={`timeline-band is-${b.kind}`} x={b.x0} y={HEIGHT - 4} width={Math.max(1, b.x1 - b.x0)} height={4} />
            ))}
            {track.bands.map((b) => (
              <rect key={`f-${b.kind}-${b.x0.toFixed(1)}`} className={`timeline-fill is-${b.kind}`} x={b.x0} y={0} width={Math.max(1, b.x1 - b.x0)} height={HEIGHT - 4} />
            ))}
            {!live ? <rect className="timeline-later" x={thumbX} y={0} width={Math.max(0, track.width - thumbX)} height={HEIGHT} /> : null}
            <path className="timeline-line" d={track.path} />
            {track.markers.map((m) => (
              <path
                key={`${m.kind}-${m.index}`}
                className={`timeline-marker is-${m.kind}`}
                d={m.kind === 'high' ? `M${m.x - 3.2},${m.y - 2.5}L${m.x + 3.2},${m.y - 2.5}L${m.x},${m.y - 7.5}Z` : `M${m.x - 3.2},${m.y + 2.5}L${m.x + 3.2},${m.y + 2.5}L${m.x},${m.y + 7.5}Z`}
              >
                <title>{`${m.kind === 'high' ? 'High' : 'Low'}: ${fmtPriceMwh(m.price)} at ${momentLabel(tickMs(history[m.index]), firstMs)}`}</title>
              </path>
            ))}
            <line className="timeline-thumb" x1={thumbX} x2={thumbX} y1={0} y2={HEIGHT} />
            <circle className="timeline-knob" cx={thumbX} cy={3.5} r={3.5} />
          </svg>
        ) : null}
      </div>
      <div className="timeline-caption">
        <span className="timeline-start">
          {firstMs !== null ? `From ${fmtTimeOfDay(firstMs, { compact: true, words: true })}, ${shortDate(firstMs)}` : 'Starts when the replay does'}
          {predictedX !== null ? <span className="timeline-key-predicted"> · shaded: AI prediction</span> : null}
        </span>
        <span className="timeline-now">
          {live ? (
            <>
              <span className="timeline-live-dot" aria-hidden="true" />
              Live
            </>
          ) : shownMs !== null ? (
            `Viewing ${momentLabel(shownMs, firstMs)}`
          ) : null}
        </span>
      </div>
    </div>
  );
}
