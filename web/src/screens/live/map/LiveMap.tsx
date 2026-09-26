// The live map (docs/UX.md, Live map): a dark gray full-window stage with the
// red Texas outline, the batteries as dots, clickable city labels, and the four
// corner panels. Canvas drawing lives in src/map/renderer.ts; this component
// wires it to the live data, the selection, the pointer and the keyboard.

import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type MouseEvent, type ReactNode } from 'react';
import { byBatteries, citiesStore, useCities } from '../../../lib/cities.ts';
import { fmtCount } from '../../../lib/format.ts';
import { holdMapFeed, layoutStore, statusStore, useLayoutState, useMapStatus } from '../../../map/feed.ts';
import type { Padding } from '../../../map/geometry.ts';
import { MapRenderer, type CityBox } from '../../../map/renderer.ts';
import { dispatchSelection, escapeClears, useSelectedCity, type SelectionAction } from '../../../map/selection.ts';
import { CHARGING, DOWN, OFFLINE, SELLING } from '../../../map/status.ts';
import { useTechnical } from '../../../state/prefs.ts';
import { ClockCorner, ControlsCorner, FleetCorner, MoneyCorner } from './corners.tsx';
import { PREFERRED_SIDES, placeLabels, type LabelItem, type Rect } from './labels.ts';

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

/**
 * A small margin around Texas, a little more below it for the caption. The scope chip sits over
 * Oklahoma and the corner panels over New Mexico, Oklahoma, Mexico and the Gulf.
 */
function mapPadding(width: number, height: number): Padding {
  const side = Math.max(16, Math.round(width * 0.012));
  // Narrow layout (the panels stack below the map): the chip is centred, so it needs room above Texas.
  const top = width < 860 ? 56 : Math.max(16, Math.round(height * 0.022));
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

/** The canvas's text alternative, kept up to date without re-rendering the whole map every frame. */
function MapSummary({ canvas }: { canvas: { current: HTMLCanvasElement | null } }) {
  const fleet = useMapStatus((s) => s.fleet);
  const cities = useCities();
  useEffect(() => {
    const total = cities.reduce((a, c) => a + c.batteries, 0);
    const names = byBatteries(cities).map((c) => c.name);
    const text =
      `Map of Texas with a red outline. Dots show where the ${fmtCount(total)} batteries are, in ${names.join(', ')}: ` +
      `${fmtCount(fleet[SELLING])} selling (bright green), ${fmtCount(fleet[CHARGING])} charging (green with a light ring), ` +
      `${fmtCount(fleet[OFFLINE])} offline (gray)` +
      (fleet[DOWN] ? `, ${fmtCount(fleet[DOWN])} waiting for a controller (dark)` : '') +
      '. Use the city buttons to see one city.';
    canvas.current?.setAttribute('aria-label', text);
  }, [fleet, cities, canvas]);
  return null;
}

interface Props {
  onOpenAvailability: () => void;
  onShowCharts: () => void;
  /** The run and connection notice (idle, finished, can't reach the simulator), if any. */
  notice: ReactNode;
}

export function LiveMap({ onOpenAvailability, onShowCharts, notice }: Props) {
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
  const labelRefs = useRef(new Map<string, HTMLButtonElement>());
  const boxesRef = useRef<CityBox[]>([]);

  const selected = useSelectedCity();
  const cities = useCities();
  const { layout, phase: layoutPhase } = useLayoutState();
  const reduced = useReducedMotion();
  const technical = useTechnical();
  const [hover, setHover] = useState<string | null>(null);
  const [, setBoxesVersion] = useState(0);
  const [stats, setStats] = useState('');

  const select = useCallback((action: SelectionAction) => {
    const known = (layoutStore.get().layout?.cities ?? citiesStore.get()).map((c) => c.location);
    dispatchSelection(action, known);
  }, []);
  const onPriceRow = useCallback((location: string) => select({ type: 'label', location }), [select]);

  useEffect(() => holdMapFeed(), []);

  // ---- labels ---------------------------------------------------------------------

  const placeLabelsNow = useCallback(() => {
    const area = areaRef.current;
    const l = layoutStore.get().layout;
    if (!area || !l) return;
    const obstacles = [tlRef.current, trRef.current, blRef.current, brRef.current, scopeRef.current, captionRef.current, noticeRef.current]
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

  // After every render that may move or resize a label, and whenever a corner panel changes size.
  useLayoutEffect(() => {
    placeLabelsNow();
  });

  useEffect(() => {
    const ro = new ResizeObserver(() => placeLabelsNow());
    for (const el of [tlRef.current, trRef.current, blRef.current, brRef.current]) if (el) ro.observe(el);
    return () => ro.disconnect();
  }, [placeLabelsNow]);

  // ---- the renderer --------------------------------------------------------------

  useEffect(() => {
    const base = baseRef.current;
    const dots = dotsRef.current;
    const area = areaRef.current;
    if (!base || !dots || !area) return;
    const renderer = new MapRenderer(base, dots);
    rendererRef.current = renderer;

    const refreshBoxes = () => {
      boxesRef.current = renderer.cityBoxes();
      const vp = renderer.getViewport();
      if (vp) {
        area.style.setProperty('--map-left', `${vp.left}px`);
        area.style.setProperty('--map-top', `${vp.top}px`);
        area.style.setProperty('--map-width', `${vp.width}px`);
        area.style.setProperty('--map-height', `${vp.height}px`);
      }
      setBoxesVersion((v) => v + 1);
    };

    const resize = () => {
      const w = area.clientWidth;
      const h = area.clientHeight;
      if (!w || !h) return;
      renderer.resize(w, h, window.devicePixelRatio || 1, mapPadding(w, h), MAP_ALIGN_X);
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

  // Technical details: how big and how fast the map is.
  useEffect(() => {
    if (!technical) return;
    const tick = () => {
      const r = rendererRef.current;
      const l = layoutStore.get();
      const s = statusStore.get();
      if (!r) return;
      setStats(
        `${fmtCount(r.stats.dots)} dots · paint ${r.stats.paintMs.toFixed(1)} ms (slowest ${r.stats.maxPaintMs.toFixed(1)}) · ` +
          `decode + restyle ${(r.stats.updateMs + s.decodeMs).toFixed(1)} ms per frame · cells ${r.stats.buildMs.toFixed(1)} ms · ` +
          `battery_status ${fmtCount(s.statusChars)} chars · layout ${l.layout?.version ?? 'none'} (${fmtCount(l.positionsChars)} chars)`,
      );
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [technical]);

  // ---- input ------------------------------------------------------------------------

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (escapeClears(e)) select({ type: 'escape' });
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

  const onMapClick = (e: MouseEvent<HTMLDivElement>) => select({ type: 'map', location: cityAtEvent(e) });

  const onMapMove = (e: MouseEvent<HTMLDivElement>) => {
    const city = cityAtEvent(e);
    if (city !== hover) setHover(city);
  };

  const selectedCity = selected === null ? null : (cities.find((c) => c.location === selected) ?? null);

  return (
    <div className="livemap">
      <h1 className="sr-only">Live map</h1>

      <div className="corner-slot corner-slot-tl" ref={tlRef}>
        <ClockCorner scope={selected} onSelect={onPriceRow} />
      </div>
      <div className="corner-slot corner-slot-tr" ref={trRef}>
        <MoneyCorner scope={selected} />
      </div>

      <div className="map-area" ref={areaRef}>
        {/* Mouse users can click a city's dots, or empty map to go back to all of Texas; keyboard users have
            the city buttons, the All of Texas chip and Esc. */}
        {/* oxlint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- the city buttons and Esc do the same from the keyboard */}
        <div
          className={`map-canvas${hover ? ' is-pointing' : ''}`}
          ref={canvasWrapRef}
          onClick={onMapClick}
          onMouseMove={onMapMove}
          onMouseLeave={() => setHover(null)}
        >
          <canvas ref={baseRef} className="map-layer" aria-hidden="true" />
          <canvas ref={dotsRef} className="map-layer" role="img" aria-label="Map of Texas with the batteries as dots" />
          <MapSummary canvas={dotsRef} />
        </div>

        <div className="map-scope" ref={scopeRef} role="group" aria-label="Which part of Texas the numbers show">
          <button type="button" className="scope-chip" aria-pressed={selected === null} onClick={() => select({ type: 'all' })}>
            All of Texas
          </button>
          <span className="scope-hint">
            {selectedCity ? (
              <>
                Showing <strong>{selectedCity.name}</strong> · Esc for all
              </>
            ) : (
              'Click a city to see its numbers'
            )}
          </span>
        </div>

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

        {notice ? (
          <div className="map-notice" ref={noticeRef}>
            {notice}
          </div>
        ) : layoutPhase === 'error' ? (
          <div className="map-notice map-notice-quiet" ref={noticeRef} role="status">
            The battery map could not load. It tries again every few seconds.
          </div>
        ) : null}

        <p className="map-caption" ref={captionRef}>
          Battery locations are simulated inside real metro areas.
          {technical && stats ? <span className="tech">{stats}</span> : null}
        </p>
      </div>

      <div className="corner-slot corner-slot-bl" ref={blRef}>
        <ControlsCorner onOpenAvailability={onOpenAvailability} onShowCharts={onShowCharts} />
      </div>
      <div className="corner-slot corner-slot-br" ref={brRef}>
        <FleetCorner scope={selected} />
      </div>

      <div className="sr-only" role="status" aria-live="polite">
        {selectedCity ? `Showing ${selectedCity.name}: ${fmtCount(selectedCity.batteries)} batteries.` : 'Showing all of Texas.'}
      </div>
    </div>
  );
}
