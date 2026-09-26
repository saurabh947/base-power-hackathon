// The live map (docs/UX.md, Live map and "Live map v2"): a dark gray full-window stage with the red Texas
// outline, the batteries as dots, clickable city labels, one zoom level (all of Texas or the Texas Triangle),
// the timeline, and the four corner panels. Canvas drawing lives in src/map/renderer.ts; this component wires
// it to the live data, the selection, the zoom, the timeline, the pointer and the keyboard.

import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type MouseEvent, type ReactNode } from 'react';
import { Icon } from '../../../components/Icon.tsx';
import { liveStore, useLive } from '../../../live/liveStore.ts';
import { stepFrom } from '../../../live/past.ts';
import { backToLive, LiveDataContext, recorder, shownStore, useViewing, viewAt, viewingStore } from '../../../live/timeTravel.ts';
import { byBatteries, citiesStore, useCities } from '../../../lib/cities.ts';
import { fmtCount, fmtTimeOfDay } from '../../../lib/format.ts';
import { holdMapFeed, layoutStore, statusStore, useLayoutState, useMapStatus } from '../../../map/feed.ts';
import type { Padding } from '../../../map/geometry.ts';
import { MapRenderer, type CityBox } from '../../../map/renderer.ts';
import { dispatchSelection, escapeClears, useSelectedCity, type SelectionAction } from '../../../map/selection.ts';
import { CHARGING, DOWN, OFFLINE, SELLING, WAITING } from '../../../map/status.ts';
import { zoomPadding } from '../../../map/viewports.ts';
import { useTechnical } from '../../../state/prefs.ts';
import { ClockCorner, ControlsCorner, FleetCorner, MoneyCorner } from './corners.tsx';
import { PREFERRED_SIDES, placeLabels, type LabelItem, type Rect } from './labels.ts';
import { momentLabel } from './timeline.ts';

const reducedQuery = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

function useReducedMotion(): boolean {
  return useSyncExternalStore(
    (cb) => {
      reducedQuery?.addEventListener('change', cb);
      return () => reducedQuery?.removeEventListener('change', cb);
    },
    () => !!reducedQuery?.matches,
    () => false,
  );
}

/** Narrow windows stack the corners under the map (map.css, max-width 860 px). */
const STACKED_MAX_WIDTH = 860;

/**
 * A small margin around Texas, a little more below it for the caption. The scope chip sits over
 * Oklahoma and the corner panels over New Mexico, Oklahoma, Mexico and the Gulf.
 */
function mapPadding(width: number, height: number): Padding {
  const side = Math.max(16, Math.round(width * 0.012));
  // Narrow layout (the panels stack below the map): the chip is centred, so it needs room above Texas.
  const top = width < STACKED_MAX_WIDTH ? 56 : Math.max(16, Math.round(height * 0.022));
  return { top, right: side, bottom: Math.max(28, Math.round(height * 0.038)), left: side };
}

/**
 * Texas is fuller on its east side (the Louisiana border, Houston, the coast) than on its west
 * (the panhandle and the El Paso tip), so spare width goes more to the right, where the right-hand
 * panels need it.
 */
const MAP_ALIGN_X = 0.38;

function rectOf(el: HTMLElement | null): Rect | null {
  if (!el || el.offsetWidth === 0) return null;
  return { left: el.offsetLeft, top: el.offsetTop, right: el.offsetLeft + el.offsetWidth, bottom: el.offsetTop + el.offsetHeight };
}

/** Keys the map handles itself only when nothing else wants them (a field, a menu, a dialog, a slider). */
function mapKeyTarget(e: KeyboardEvent): boolean {
  if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return false;
  const el = e.target as { tagName?: string; isContentEditable?: boolean; closest?: (s: string) => unknown; getAttribute?: (a: string) => string | null } | null;
  const tag = el?.tagName?.toLowerCase();
  if (tag === 'input' || tag === 'select' || tag === 'textarea' || el?.isContentEditable) return false;
  if (el?.closest?.('dialog[open], [role="dialog"], [role="slider"], [role="radiogroup"]')) return false;
  return true;
}

/** The canvas's text alternative, kept up to date without re-rendering the whole map every frame. */
function MapSummary({ canvas }: { canvas: { current: HTMLCanvasElement | null } }) {
  const fleet = useMapStatus((s) => s.fleet);
  const cities = useCities();
  const viewing = useViewing();
  useEffect(() => {
    const total = cities.reduce((a, c) => a + c.batteries, 0);
    const names = byBatteries(cities).map((c) => c.name);
    const text =
      `Map of Texas with a red outline${viewing === null ? '' : `, as it was at ${fmtTimeOfDay(viewing)}`}. Dots show where the ${fmtCount(total)} batteries are, in ${names.join(', ')}: ` +
      `${fmtCount(fleet[SELLING])} selling (green), ${fmtCount(fleet[CHARGING])} charging (blue), ${fmtCount(fleet[WAITING])} waiting (small gray), ` +
      `${fmtCount(fleet[OFFLINE])} offline (red rings)` +
      (fleet[DOWN] ? `, ${fmtCount(fleet[DOWN])} whose controller crashed (pulsing red)` : '') +
      '. Use the city buttons to see one city, and plus or minus to zoom.';
    canvas.current?.setAttribute('aria-label', text);
  }, [fleet, cities, canvas, viewing]);
  return null;
}

/** "Viewing 7:45 pm · Back to live", over the map while the timeline shows an earlier moment. */
function ViewingChip({ chipRef }: { chipRef: { current: HTMLDivElement | null } }) {
  const viewing = useViewing();
  const firstTick = useLive((s) => s.history[0]?.ts_utc ?? null);
  const source = useMapStatus((s) => s.source);
  if (viewing === null) return null;
  const first = firstTick ? Date.parse(firstTick) : null;
  return (
    <div className="viewing-chip" ref={chipRef} role="status">
      <div className="viewing-chip-row">
        <Icon name="clock" size={15} />
        <span>
          Viewing <strong>{momentLabel(viewing, first)}</strong>
        </span>
        <span className="viewing-sep" aria-hidden="true">
          ·
        </span>
        <button
          type="button"
          className="linklike viewing-back"
          onClick={() => {
            backToLive();
            // The chip goes away with its button: keep the keyboard on the timeline.
            document.querySelector<HTMLElement>('.timeline-track')?.focus();
          }}
        >
          Back to live
        </button>
      </div>
      {source === 'city_level' ? (
        <p className="viewing-hint">Before you opened this page: the map shows each city as a whole, not each battery.</p>
      ) : null}
    </div>
  );
}

interface Props {
  onOpenAvailability: () => void;
  /** The run and connection notice (idle, finished, can't reach the simulator), if any. */
  notice: ReactNode;
}

/** Space kept between the two right-hand corners, and between them and the map's top and bottom edges. */
const RIGHT_GAP = 10;
const EDGE = 12;

export function LiveMap({ onOpenAvailability, notice }: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const areaRef = useRef<HTMLDivElement>(null);
  const canvasWrapRef = useRef<HTMLDivElement>(null);
  const baseRef = useRef<HTMLCanvasElement>(null);
  const dotsRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<MapRenderer | null>(null);
  const tlRef = useRef<HTMLDivElement>(null);
  const trRef = useRef<HTMLDivElement>(null);
  const blRef = useRef<HTMLDivElement>(null);
  const brRef = useRef<HTMLDivElement>(null);
  const scopeRef = useRef<HTMLDivElement>(null);
  const captionRef = useRef<HTMLParagraphElement>(null);
  const noticeRef = useRef<HTMLDivElement>(null);
  const chipRef = useRef<HTMLDivElement>(null);
  const labelRefs = useRef(new Map<string, HTMLButtonElement>());
  const boxesRef = useRef<CityBox[]>([]);

  const selected = useSelectedCity();
  const cities = useCities();
  const { layout, phase: layoutPhase } = useLayoutState();
  const reduced = useReducedMotion();
  const technical = useTechnical();
  const viewing = useViewing();
  const [hover, setHover] = useState<string | null>(null);
  const [zoomed, setZoomed] = useState(false);
  const [zooming, setZooming] = useState(false);
  const [, setBoxesVersion] = useState(0);
  const [stats, setStats] = useState('');

  const select = useCallback((action: SelectionAction) => {
    const known = (layoutStore.get().layout?.cities ?? citiesStore.get()).map((c) => c.location);
    dispatchSelection(action, known);
  }, []);
  const onPriceRow = useCallback((location: string) => select({ type: 'label', location }), [select]);

  useEffect(() => holdMapFeed(), []);
  // Leaving the map (for another page) goes back to live: only the map has the timeline.
  useEffect(() => () => backToLive(), []);

  // ---- labels ---------------------------------------------------------------------

  const placeLabelsNow = useCallback(() => {
    const area = areaRef.current;
    const l = layoutStore.get().layout;
    if (!area || !l) return;
    const obstacles = [tlRef.current, trRef.current, blRef.current, brRef.current, scopeRef.current, captionRef.current, noticeRef.current, chipRef.current]
      .map(rectOf)
      .filter((r): r is Rect => r !== null);
    const items: (LabelItem & { batteries: number })[] = [];
    for (const box of boxesRef.current) {
      const city = l.cities[box.city];
      const el = city ? labelRefs.current.get(city.location) : undefined;
      if (!city || !el) continue;
      items.push({
        key: city.location,
        batteries: city.batteries,
        box,
        width: el.offsetWidth,
        height: el.offsetHeight,
        prefer: PREFERRED_SIDES[city.location] ?? ['e', 'w', 'n', 's'],
      });
    }
    items.sort((a, b) => b.batteries - a.batteries);
    const spots = placeLabels(items, obstacles, { left: 6, top: 6, right: area.clientWidth - 6, bottom: area.clientHeight - 6 }, 7);
    for (const [location, el] of labelRefs.current) {
      const r = spots.get(location);
      if (!r) {
        el.style.visibility = 'hidden';
        continue;
      }
      el.style.transform = `translate(${Math.round(r.left)}px, ${Math.round(r.top)}px)`;
      el.style.visibility = 'visible';
    }
  }, []);

  /**
   * The money corner (top right, which also holds the race) and the fleet corner (bottom right) share the
   * right edge. In a short window, cap the money corner's height at the room the fleet corner leaves, so the
   * two never overlap; it scrolls inside only then (css: .corner-slot-tr > .corner).
   */
  const fitRightCorners = useCallback(() => {
    const root = rootRef.current;
    const br = brRef.current;
    if (!root || !br) return;
    const room = root.clientHeight - br.offsetHeight - 2 * EDGE - RIGHT_GAP;
    root.style.setProperty('--corner-tr-max', `${Math.max(180, Math.floor(room))}px`);
    // The corners' width, for what sits between them: the scope pill, and (zoomed in, when the Triangle fills
    // the middle) a notice as a compact bar at the bottom centre (css: .livemap.is-zoomed .map-notice).
    const tl = tlRef.current;
    if (tl) root.style.setProperty('--corner-width', `${tl.offsetWidth}px`);
  }, []);

  // After every render that may move or resize a label, and whenever a corner panel changes size.
  useLayoutEffect(() => {
    fitRightCorners();
    placeLabelsNow();
  });

  useEffect(() => {
    const ro = new ResizeObserver(() => {
      fitRightCorners();
      placeLabelsNow();
    });
    for (const el of [rootRef.current, tlRef.current, trRef.current, blRef.current, brRef.current]) if (el) ro.observe(el);
    return () => ro.disconnect();
  }, [placeLabelsNow, fitRightCorners]);

  // ---- the renderer --------------------------------------------------------------

  useEffect(() => {
    const base = baseRef.current;
    const dots = dotsRef.current;
    const area = areaRef.current;
    if (!base || !dots || !area) return;

    const refreshBoxes = () => {
      const r = rendererRef.current;
      if (!r) return;
      boxesRef.current = r.cityBoxes();
      // The notice is placed against all of Texas, zoomed in or not (set on the root: it sits outside the map area).
      const vp = r.getHomeViewport();
      const root = rootRef.current;
      if (vp && root) {
        root.style.setProperty('--map-left', `${vp.left}px`);
        root.style.setProperty('--map-top', `${vp.top}px`);
        root.style.setProperty('--map-width', `${vp.width}px`);
        root.style.setProperty('--map-height', `${vp.height}px`);
      }
      setBoxesVersion((v) => v + 1);
    };

    const renderer = new MapRenderer(base, dots, {
      onZoomStart: () => setZooming(true),
      onSettle: () => {
        setZooming(false);
        refreshBoxes();
      },
    });
    rendererRef.current = renderer;

    const resize = () => {
      const w = area.clientWidth;
      const h = area.clientHeight;
      if (!w || !h) return;
      const cornerWidth = Math.max(tlRef.current?.offsetWidth ?? 0, trRef.current?.offsetWidth ?? 0, blRef.current?.offsetWidth ?? 0, brRef.current?.offsetWidth ?? 0);
      renderer.resize(w, h, window.devicePixelRatio || 1, mapPadding(w, h), MAP_ALIGN_X, zoomPadding(w, h, cornerWidth, w < STACKED_MAX_WIDTH));
      refreshBoxes();
    };
    const ro = new ResizeObserver(resize);
    ro.observe(area);
    window.addEventListener('resize', resize); // also catches devicePixelRatio changes (zoom, another screen)

    let lastLayout: unknown;
    let lastCodes: unknown;
    const apply = () => {
      const l = layoutStore.get().layout;
      if (l !== lastLayout) {
        lastLayout = l;
        renderer.setLayout(l);
        refreshBoxes();
      }
      const codes = statusStore.get().codes;
      if (codes !== lastCodes) {
        lastCodes = codes;
        renderer.setStatus(codes);
      }
    };
    resize();
    apply();
    const offLayout = layoutStore.subscribe(apply);
    const offStatus = statusStore.subscribe(apply);

    return () => {
      ro.disconnect();
      window.removeEventListener('resize', resize);
      offLayout();
      offStatus();
      renderer.destroy();
      rendererRef.current = null;
    };
  }, []);

  useEffect(() => {
    const index = selected === null || !layout ? -1 : layout.cities.findIndex((c) => c.location === selected);
    rendererRef.current?.setSelected(index < 0 ? null : index);
  }, [selected, layout]);

  useEffect(() => {
    rendererRef.current?.setReducedMotion(reduced);
  }, [reduced]);

  useEffect(() => {
    rendererRef.current?.setZoomed(zoomed);
  }, [zoomed]);

  // Technical details: how big and how fast the map is.
  useEffect(() => {
    if (!technical) return;
    const tick = () => {
      const r = rendererRef.current;
      const l = layoutStore.get();
      const s = statusStore.get();
      if (!r) return;
      setStats(
        `${fmtCount(r.stats.dots)} dots${r.stats.zoomed ? ' (zoomed in)' : ''} · paint ${r.stats.paintMs.toFixed(1)} ms (slowest ${r.stats.maxPaintMs.toFixed(1)}) · ` +
          `decode + restyle ${(r.stats.updateMs + s.decodeMs).toFixed(1)} ms per frame · cells ${r.stats.buildMs.toFixed(1)} ms · ` +
          `battery_status ${fmtCount(s.statusChars)} chars · layout ${l.layout?.version ?? 'none'} (${fmtCount(l.positionsChars)} chars) · ` +
          `timeline ${fmtCount(recorder.records.length)} frames, ${(recorder.sizeBytes / 1_048_576).toFixed(1)} MB`,
      );
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [technical]);

  // ---- input ------------------------------------------------------------------------

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (escapeClears(e)) {
        // Esc goes back to the overview: all of Texas, zoomed out.
        select({ type: 'escape' });
        setZoomed(false);
        return;
      }
      if (!mapKeyTarget(e)) return;
      if (e.key === '+' || e.key === '=' || e.code === 'NumpadAdd') {
        setZoomed(true);
        e.preventDefault();
      } else if (e.key === '-' || e.key === '_' || e.code === 'NumpadSubtract') {
        setZoomed(false);
        e.preventDefault();
      } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        // ← / → move the timeline one 5-minute step (an hour with Shift), as on the timeline itself.
        const history = liveStore.get().history;
        if (!history.length) return;
        viewAt(stepFrom(history, viewingStore.get(), (e.key === 'ArrowLeft' ? -1 : 1) * (e.shiftKey ? 12 : 1)));
        e.preventDefault();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [select]);

  const cityAtEvent = (e: MouseEvent<HTMLDivElement>): string | null => {
    const r = rendererRef.current;
    const wrap = canvasWrapRef.current;
    const l = layoutStore.get().layout;
    if (!r || !wrap || !l) return null;
    const rect = wrap.getBoundingClientRect();
    const k = r.cityAt(e.clientX - rect.left, e.clientY - rect.top);
    return k === null ? null : (l.cities[k]?.location ?? null);
  };

  const onMapClick = (e: MouseEvent<HTMLDivElement>) => {
    if (e.detail > 1) return; // the second click of a double-click zooms instead
    select({ type: 'map', location: cityAtEvent(e) });
  };

  const onMapMove = (e: MouseEvent<HTMLDivElement>) => {
    const city = cityAtEvent(e);
    if (city !== hover) setHover(city);
  };

  const selectedCity = selected === null ? null : (cities.find((c) => c.location === selected) ?? null);

  return (
    <div className={`livemap${viewing === null ? '' : ' is-past'}${zoomed ? ' is-zoomed' : ''}${zooming ? ' is-zooming' : ''}`} ref={rootRef}>
      <h1 className="sr-only">Live map</h1>

      <LiveDataContext.Provider value={shownStore}>
        <div className="corner-slot corner-slot-tl" ref={tlRef}>
          <ClockCorner scope={selected} onSelect={onPriceRow} />
        </div>
        <div className="corner-slot corner-slot-tr" ref={trRef}>
          <MoneyCorner scope={selected} />
        </div>
      </LiveDataContext.Provider>

      <div className="map-area" ref={areaRef}>
        {/* Mouse users can click a city's dots, or empty map to go back to all of Texas, and double-click to
            zoom; keyboard users have the city buttons, the All of Texas chip, the zoom buttons and Esc. */}
        {/* oxlint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- the city buttons, the zoom buttons and Esc do the same from the keyboard */}
        <div
          className={`map-canvas${hover ? ' is-pointing' : ''}`}
          ref={canvasWrapRef}
          onClick={onMapClick}
          onDoubleClick={() => setZoomed((z) => !z)}
          onMouseMove={onMapMove}
          onMouseLeave={() => setHover(null)}
        >
          <canvas ref={baseRef} className="map-layer" aria-hidden="true" />
          <canvas ref={dotsRef} className="map-layer" role="img" aria-label="Map of Texas with the batteries as dots" />
          <MapSummary canvas={dotsRef} />
        </div>

        <div className="map-scope" ref={scopeRef} role="group" aria-label="Which part of Texas the map and numbers show">
          <button type="button" className="scope-chip" aria-pressed={selected === null} onClick={() => select({ type: 'all' })}>
            All of Texas
          </button>
          <span className="scope-hint">
            {selectedCity ? (
              <>
                Showing <strong>{selectedCity.name}</strong> · Esc for all
              </>
            ) : zoomed ? (
              'Zoomed in on the four cities'
            ) : (
              'Click a city to see its numbers'
            )}
          </span>
          <span className="zoom-control" role="group" aria-label="Zoom">
            <button
              type="button"
              className="zoom-btn"
              onClick={() => setZoomed(true)}
              disabled={zoomed}
              aria-label="Zoom in to the Texas Triangle"
              title="Zoom in to the four cities (or double-click the map)"
            >
              <Icon name="plus" size={16} />
            </button>
            <button
              type="button"
              className="zoom-btn"
              onClick={() => setZoomed(false)}
              disabled={!zoomed}
              aria-label="Zoom out to all of Texas"
              title="Zoom out to all of Texas (or press Esc)"
            >
              <Icon name="minus" size={16} />
            </button>
          </span>
        </div>

        <ViewingChip chipRef={chipRef} />

        {cities.map((c) => {
          const on = selected === c.location;
          return (
            <button
              key={c.location}
              type="button"
              ref={(el) => {
                if (el) labelRefs.current.set(c.location, el);
                else labelRefs.current.delete(c.location);
              }}
              className={`city-label${on ? ' is-selected' : ''}${selected !== null && !on ? ' is-dimmed' : ''}${hover === c.location ? ' is-hover' : ''}`}
              aria-pressed={on}
              style={{ visibility: 'hidden' }}
              onClick={() => select({ type: 'label', location: c.location })}
              onMouseEnter={() => setHover(c.location)}
              onMouseLeave={() => setHover(null)}
            >
              <span className="city-name">{c.name}</span>
              <span className="city-count">
                {fmtCount(c.batteries)} batteries
                {technical ? <span className="tech"> {c.location}</span> : null}
              </span>
            </button>
          );
        })}

        <p className="map-caption" ref={captionRef}>
          Battery locations are simulated inside real metro areas.
          {technical && stats ? <span className="tech">{stats}</span> : null}
        </p>
      </div>

      {/* Over West Texas on a wide screen; in narrow windows, in the column under the map. */}
      {notice ? (
        <div className="map-notice" ref={noticeRef}>
          {notice}
        </div>
      ) : layoutPhase === 'error' ? (
        <div className="map-notice map-notice-quiet" ref={noticeRef} role="status">
          The battery map could not load. It tries again every few seconds.
        </div>
      ) : null}

      <div className="corner-slot corner-slot-bl" ref={blRef}>
        <ControlsCorner onOpenAvailability={onOpenAvailability} />
      </div>
      <LiveDataContext.Provider value={shownStore}>
        <div className="corner-slot corner-slot-br" ref={brRef}>
          <FleetCorner scope={selected} />
        </div>
      </LiveDataContext.Provider>

      <div className="sr-only" role="status" aria-live="polite">
        {selectedCity ? `Showing ${selectedCity.name}: ${fmtCount(selectedCity.batteries)} batteries.` : 'Showing all of Texas.'}
        {zoomed ? ' Zoomed in on the Texas Triangle.' : ''}
      </div>
    </div>
  );
}
