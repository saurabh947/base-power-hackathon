// Draws the live map on two stacked canvases: the red Texas outline (redrawn on resize and while zooming)
// and the battery dots (redrawn on each 1 Hz frame). Between frames nothing is drawn, except while a
// transition, a crash pulse, a hand-over flash or the zoom is playing; then a requestAnimationFrame loop runs
// until everything has settled. prefers-reduced-motion: no halos, pulses, transitions or zoom animation.
//
// Dot looks (src/map/looks.ts, docs/UX.md "Live map v2"): green solid selling, blue solid charging, small gray
// waiting, a hollow red ring offline, and red pulsing while the controller is down. Each dot takes its
// batteries' majority look, and any offline or crashed batteries in it add a red edge, so an outage always
// shows where it is. When a crashed controller's batteries are handed over, their dots flash once and return
// to their normal look.
//
// Zoom (src/map/viewports.ts): exactly one level, all of Texas or the Texas Triangle. The zoom scales the
// current dots smoothly and cross-fades to dots re-aggregated for the new scale.

import { aggregateCodes, buildCells, nearestCell, spacingFor, type Cells } from './cells.ts';
import { fitBBox, toScreenX, toScreenY, type Padding, type Viewport } from './geometry.ts';
import { NO_CITY, type MapLayout } from './layout.ts';
import { allocLooks, computeLooks, edgeAlpha, edgeWidth, LOOK_KEYS, PALETTE, type DotLooks } from './looks.ts';
import { CODES, DOWN, OFFLINE, WAITING } from './status.ts';
import { TEXAS_BBOX, TEXAS_OUTLINE } from './texasOutline.ts';
import { easeInOut, lerpViewport, screenMap, triangleBBox, triangleViewport, ZOOM_MS } from './viewports.ts';

export const OUTLINE_COLOR = '#ff3b3b';

/** Zoomed-in dots sit on a slightly wider grid, so they are clearly visible (still far more of them). */
export const ZOOM_SPACING = 1.3;

export interface CityBox {
  city: number;
  left: number;
  top: number;
  right: number;
  bottom: number;
  /** Battery-weighted centre of the city's dots. */
  cx: number;
  cy: number;
}

export interface RenderStats {
  /** Dots on screen. */
  dots: number;
  /** Batteries shown as "controller down" / offline right now. */
  down: number;
  offline: number;
  /** Last paint, and the slowest of the last 120. */
  paintMs: number;
  maxPaintMs: number;
  /** Last per-frame update: re-counting codes per cell and computing each dot's look. */
  updateMs: number;
  /** Last rebuild of the cells (layout, resize or zoom). */
  buildMs: number;
  paints: number;
  animating: boolean;
  zoomed: boolean;
}

const TAU_LOOK_MS = 160;
const PULSE_MS = 900;
const RELIGHT_MS = 900;
const GLOW_SPRITE = 64;
/** How strong the selling halo is: enough to read as "lit up", not so much that dense cores wash out. */
const GLOW_ALPHA = 0.22;
const TWO_PI = Math.PI * 2;

function makeGlowSprite(): HTMLCanvasElement | null {
  if (typeof document === 'undefined') return null;
  const c = document.createElement('canvas');
  c.width = c.height = GLOW_SPRITE;
  const ctx = c.getContext('2d');
  if (!ctx) return null;
  const [r, g, b] = PALETTE.glow;
  const grad = ctx.createRadialGradient(GLOW_SPRITE / 2, GLOW_SPRITE / 2, 0, GLOW_SPRITE / 2, GLOW_SPRITE / 2, GLOW_SPRITE / 2);
  grad.addColorStop(0, `rgba(${r},${g},${b},0.9)`);
  grad.addColorStop(0.35, `rgba(${r},${g},${b},0.32)`);
  grad.addColorStop(1, `rgba(${r},${g},${b},0)`);
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, GLOW_SPRITE, GLOW_SPRITE);
  return c;
}

const colorCache = new Map<number, string>();
function rgb(r: number, g: number, b: number): string {
  const key = ((r & 255) << 16) | ((g & 255) << 8) | (b & 255);
  let s = colorCache.get(key);
  if (!s) {
    s = `rgb(${r & 255},${g & 255},${b & 255})`;
    if (colorCache.size > 4096) colorCache.clear();
    colorCache.set(key, s);
  }
  return s;
}

const OUTAGE = rgb(...PALETTE.outage);

function approach(current: number, target: number, dtMs: number, tauMs: number): number {
  return target + (current - target) * Math.exp(-dtMs / tauMs);
}

/** One aggregation of the batteries into dots, built for one viewport. */
interface DotSet {
  vp: Viewport;
  cells: Cells;
  counts: Uint32Array | null;
  target: DotLooks;
  current: DotLooks;
  /** Per dot: when its crashed batteries were handed back (NaN = never). */
  relitStartMs: Float64Array;
  boxes: CityBox[];
}

interface ZoomAnim {
  from: Viewport;
  to: Viewport;
  startMs: number;
  next: DotSet | null;
}

export interface RendererEvents {
  /** A zoom started: the labels hide until it settles. */
  onZoomStart?: () => void;
  /** The view settled (after a zoom, a resize or a new layout): place the labels again. */
  onSettle?: () => void;
}

export class MapRenderer {
  readonly stats: RenderStats = {
    dots: 0,
    down: 0,
    offline: 0,
    paintMs: 0,
    maxPaintMs: 0,
    updateMs: 0,
    buildMs: 0,
    paints: 0,
    animating: false,
    zoomed: false,
  };

  private readonly base: HTMLCanvasElement;
  private readonly dots: HTMLCanvasElement;
  private readonly glow: HTMLCanvasElement | null;
  private readonly events: RendererEvents;
  private width = 0;
  private height = 0;
  private dpr = 1;
  private homePadding: Padding = { top: 16, right: 16, bottom: 16, left: 16 };
  private zoomPad: Padding = { top: 16, right: 16, bottom: 16, left: 16 };
  private alignX = 0.5;
  private home: Viewport | null = null;
  private triangle: Viewport | null = null;
  private zoomed = false;
  private camera: Viewport | null = null;
  private layout: MapLayout | null = null;
  private set: DotSet | null = null;
  private anim: ZoomAnim | null = null;
  private codes: Uint8Array | null = null;
  private anyPulse = false;
  private selected: number | null = null;
  private reduced = false;
  private raf = 0;
  private lastStepMs = 0;
  private pulseStartMs = 0;
  private recentPaints: number[] = [];

  constructor(base: HTMLCanvasElement, dots: HTMLCanvasElement, events: RendererEvents = {}) {
    this.base = base;
    this.dots = dots;
    this.events = events;
    this.glow = makeGlowSprite();
  }

  destroy(): void {
    if (this.raf > 0) cancelAnimationFrame(this.raf);
    this.raf = 0;
  }

  setReducedMotion(on: boolean): void {
    if (on === this.reduced) return;
    this.reduced = on;
    if (on && this.anim) this.finishZoom();
    this.paintBase();
    this.update(false);
  }

  setLayout(layout: MapLayout | null): void {
    this.layout = layout;
    this.fitViews();
    this.rebuild();
  }

  /** Status codes for every battery (null: no data, shown as online and waiting). */
  setStatus(codes: Uint8Array | null): void {
    this.codes = codes;
    this.update(true);
  }

  setSelected(city: number | null): void {
    if (city === this.selected) return;
    this.selected = city;
    this.update(true);
  }

  /**
   * Fit Texas into `width` × `height` CSS px minus `homePadding` (`alignX` splits spare width, see fitBBox), and
   * the Texas Triangle minus `zoomPadding` for the zoomed-in view.
   */
  resize(width: number, height: number, dpr: number, homePadding: Padding, alignX: number, zoomPadding: Padding): void {
    const same =
      width === this.width &&
      height === this.height &&
      dpr === this.dpr &&
      alignX === this.alignX &&
      JSON.stringify(homePadding) === JSON.stringify(this.homePadding) &&
      JSON.stringify(zoomPadding) === JSON.stringify(this.zoomPad);
    if (same) return;
    this.width = width;
    this.height = height;
    this.dpr = dpr;
    this.homePadding = homePadding;
    this.zoomPad = zoomPadding;
    this.alignX = alignX;
    for (const canvas of [this.base, this.dots]) {
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
    }
    this.fitViews();
    this.rebuild();
  }

  /** Zoom to the Texas Triangle (true) or back to all of Texas (false), animated unless `animate` is false. */
  setZoomed(on: boolean, animate = true): void {
    if (on === this.zoomed) return;
    this.zoomed = on;
    this.stats.zoomed = on;
    const target = this.targetView();
    if (!target || !this.camera || !this.layout) {
      this.rebuild();
      return;
    }
    if (!animate || this.reduced) {
      this.anim = null;
      this.camera = target;
      this.paintBase();
      this.rebuild();
      return;
    }
    const from = this.camera;
    const next = this.buildSet(target);
    if (next) this.copyTargets(next);
    this.anim = { from, to: target, startMs: performance.now(), next };
    this.events.onZoomStart?.();
    this.startLoop();
  }

  isZoomed(): boolean {
    return this.zoomed;
  }

  isZooming(): boolean {
    return this.anim !== null;
  }

  /** The all-of-Texas view: where the page's own overlays (the scope chip, notices) are placed. */
  getHomeViewport(): Viewport | null {
    return this.home;
  }

  /** The view on screen now. */
  getViewport(): Viewport | null {
    return this.camera;
  }

  /** Screen boxes of each city's dots (CSS px), for placing the labels. Empty while zooming. */
  cityBoxes(): CityBox[] {
    return this.anim ? [] : (this.set?.boxes ?? []);
  }

  /** The city whose dots are under (x, y), or null. */
  cityAt(x: number, y: number): number | null {
    const set = this.set;
    if (!set || this.anim) return null;
    const cell = nearestCell(set.cells, x, y, 7);
    if (cell < 0) return null;
    const city = set.cells.city[cell];
    return city === NO_CITY ? null : city;
  }

  // ---- views and cells -----------------------------------------------------------------

  private fitViews(): void {
    if (!this.width || !this.height) return;
    this.home = fitBBox(TEXAS_BBOX, this.width, this.height, this.homePadding, this.alignX);
    const box = this.layout ? triangleBBox(this.layout.x, this.layout.y) : null;
    this.triangle = box ? triangleViewport(box, this.width, this.height, this.zoomPad) : null;
    this.anim = null;
    this.camera = this.targetView();
    this.paintBase();
  }

  private targetView(): Viewport | null {
    return this.zoomed && this.triangle ? this.triangle : this.home;
  }

  private spacing(vp: Viewport): number {
    const home = this.home ?? vp;
    const s = spacingFor(home.height);
    return vp === home ? s : s * ZOOM_SPACING;
  }

  private buildSet(vp: Viewport): DotSet | null {
    const layout = this.layout;
    if (!layout) return null;
    const t0 = performance.now();
    const sx = new Float32Array(layout.size);
    const sy = new Float32Array(layout.size);
    for (let i = 0; i < layout.size; i++) {
      sx[i] = toScreenX(vp, layout.x[i]);
      sy[i] = toScreenY(vp, layout.y[i]);
    }
    const cells = buildCells(sx, sy, layout.cityOf, this.spacing(vp), layout.cities.length);
    const set: DotSet = {
      vp,
      cells,
      counts: null,
      target: allocLooks(cells.count),
      current: allocLooks(cells.count),
      relitStartMs: new Float64Array(cells.count).fill(Number.NaN),
      boxes: [],
    };
    this.computeSet(set, false);
    set.boxes = computeBoxes(cells, layout.cities.length);
    this.stats.buildMs = performance.now() - t0;
    return set;
  }

  private rebuild(): void {
    const vp = this.camera;
    this.set = vp ? this.buildSet(vp) : null;
    if (this.set) this.copyTargets(this.set);
    this.stats.dots = this.set?.cells.count ?? 0;
    this.paintDots(performance.now());
    this.events.onSettle?.();
    if (this.anyPulse) this.startLoop();
  }

  // ---- looks -------------------------------------------------------------------------

  private codesFor(size: number): Uint8Array {
    return this.codes && this.codes.length === size ? this.codes : new Uint8Array(size).fill(WAITING);
  }

  /** Recompute a set's target looks; with `detectRelight`, start a flash where crashed batteries came back. */
  private computeSet(set: DotSet, detectRelight: boolean): void {
    const codes = this.codesFor(set.cells.cellOf.length);
    set.counts = aggregateCodes(set.cells, codes, set.counts ?? undefined);
    const prevDown = detectRelight ? set.target.down.slice() : null;
    computeLooks(set.cells, set.counts, { selected: this.selected, reducedMotion: this.reduced }, set.target);
    if (prevDown && !this.reduced) {
      const now = performance.now();
      for (let c = 0; c < set.cells.count; c++) {
        if (prevDown[c] - set.target.down[c] > 0.001) set.relitStartMs[c] = now;
      }
    }
  }

  private copyTargets(set: DotSet): void {
    for (const key of LOOK_KEYS) set.current[key].set(set.target[key]);
    set.relitStartMs.fill(Number.NaN);
  }

  /** Recompute every dot's target look; animate towards it (or jump, when `animate` is false). */
  private update(animate: boolean): void {
    const set = this.set;
    if (!set) return;
    const t0 = performance.now();
    this.computeSet(set, animate);
    if (this.anim?.next) {
      this.computeSet(this.anim.next, false);
      this.copyTargets(this.anim.next);
    }
    let down = 0;
    let offline = 0;
    let pulse = false;
    const counts = set.counts;
    for (let c = 0; c < set.cells.count; c++) {
      if (counts) {
        down += counts[c * CODES + DOWN];
        offline += counts[c * CODES + OFFLINE];
      }
      if (set.target.down[c] > 0) pulse = true;
    }
    if (pulse && !this.anyPulse) this.pulseStartMs = performance.now();
    this.anyPulse = pulse;
    this.stats.down = down;
    this.stats.offline = offline;
    this.stats.updateMs = performance.now() - t0;

    if (!animate || this.reduced) {
      this.copyTargets(set);
      if (!this.anim) this.paintDots(performance.now());
      if (this.anim || (pulse && !this.reduced)) this.startLoop();
      return;
    }
    this.startLoop();
  }

  // ---- the animation loop ---------------------------------------------------------------

  private startLoop(): void {
    // Nonzero while a frame is scheduled, and -1 while one runs (it decides itself whether to go on).
    if (this.raf) return;
    this.lastStepMs = performance.now();
    this.stats.animating = true;
    const loop = (now: number) => {
      this.raf = -1;
      const dt = Math.min(100, Math.max(0, now - this.lastStepMs));
      this.lastStepMs = now;
      let moving = this.stepZoom(now);
      if (this.step(dt, now)) moving = true;
      this.paintDots(now);
      this.raf = moving ? requestAnimationFrame(loop) : 0;
      if (!moving) this.stats.animating = false;
    };
    this.raf = requestAnimationFrame(loop);
  }

  /** Advance the zoom; true while it runs. */
  private stepZoom(now: number): boolean {
    const anim = this.anim;
    if (!anim) return false;
    const t = (now - anim.startMs) / ZOOM_MS;
    if (t >= 1) {
      this.finishZoom();
      return false;
    }
    this.camera = lerpViewport(anim.from, anim.to, easeInOut(t));
    this.paintBase();
    return true;
  }

  private finishZoom(): void {
    const anim = this.anim;
    if (!anim) return;
    this.anim = null;
    this.camera = anim.to;
    if (anim.next) this.set = anim.next;
    else this.set = this.buildSet(anim.to);
    if (this.set) this.copyTargets(this.set);
    this.stats.dots = this.set?.cells.count ?? 0;
    this.paintBase();
    this.update(false);
    this.events.onSettle?.();
  }

  /** Advance every dot towards its target; true while anything still moves, pulses or flashes. */
  private step(dtMs: number, now: number): boolean {
    const set = this.set;
    if (!set) return false;
    const t = set.target;
    const c = set.current;
    let moving = this.anyPulse && !this.reduced;
    for (let i = 0; i < set.cells.count; i++) {
      if (now - set.relitStartMs[i] < RELIGHT_MS) moving = true;
      let settled = true;
      for (const key of LOOK_KEYS) {
        const cur = c[key][i];
        const tgt = t[key][i];
        if (cur === tgt) continue;
        const next = approach(cur, tgt, dtMs, TAU_LOOK_MS);
        const eps = key === 'red' || key === 'green' || key === 'blue' ? 0.5 : key === 'radius' ? 0.02 : 0.004;
        if (Math.abs(next - tgt) > eps) {
          c[key][i] = next;
          settled = false;
        } else {
          c[key][i] = tgt;
        }
      }
      if (!settled) moving = true;
    }
    return moving;
  }

  // ---- painting ------------------------------------------------------------------

  private paintBase(): void {
    const ctx = this.base.getContext('2d');
    const vp = this.camera;
    if (!ctx || !vp) return;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);
    ctx.beginPath();
    for (let i = 0; i < TEXAS_OUTLINE.length; i += 2) {
      const x = toScreenX(vp, TEXAS_OUTLINE[i]);
      const y = toScreenY(vp, TEXAS_OUTLINE[i + 1]);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.strokeStyle = OUTLINE_COLOR;
    if (!this.reduced && !this.anim) {
      ctx.save();
      ctx.shadowColor = 'rgba(255, 59, 59, 0.55)';
      ctx.shadowBlur = 12;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.restore();
    }
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  private paintDots(now: number): void {
    const ctx = this.dots.getContext('2d');
    if (!ctx) return;
    const t0 = performance.now();
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);
    const camera = this.camera;
    if (!camera) return;
    const anim = this.anim;
    if (anim && anim.next) {
      // The zoom: the old dots scale with the view, then give way to dots re-aggregated for the new scale.
      const u = easeInOut(Math.min(1, (now - anim.startMs) / ZOOM_MS));
      const fade = Math.min(1, Math.max(0, (u - 0.3) / 0.6));
      if (this.set && fade < 1) this.paintSet(ctx, this.set, camera, 1 - fade, now);
      if (fade > 0) this.paintSet(ctx, anim.next, camera, fade, now);
    } else if (this.set) {
      this.paintSet(ctx, this.set, camera, 1, now);
    }
    ctx.globalAlpha = 1;

    const ms = performance.now() - t0;
    this.stats.paintMs = ms;
    this.stats.paints += 1;
    this.recentPaints.push(ms);
    if (this.recentPaints.length > 120) this.recentPaints.shift();
    this.stats.maxPaintMs = Math.max(...this.recentPaints);
  }

  private paintSet(ctx: CanvasRenderingContext2D, set: DotSet, camera: Viewport, opacity: number, now: number): void {
    const cells = set.cells;
    const c = set.current;
    const same = set.vp === camera;
    const m = same ? { k: 1, ox: 0, oy: 0 } : screenMap(set.vp, camera);
    // While zooming, dots grow with the view, but less than the map does (their count per dot is falling).
    const rk = same ? 1 : Math.sqrt(m.k);
    const n = cells.count;
    const xs = new Float32Array(n);
    const ys = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      xs[i] = m.k * cells.x[i] + m.ox;
      ys[i] = m.k * cells.y[i] + m.oy;
    }
    const pulsePhase = this.reduced ? 0 : (((now - this.pulseStartMs) / PULSE_MS) % 1 + 1) % 1;
    // Between 45% and 100%: always there, clearly beating.
    const throb = this.reduced ? 1 : 0.725 + 0.275 * Math.cos(TWO_PI * pulsePhase);

    // 1. The halo under selling dots: added light on the dark page.
    if (!this.reduced && this.glow) {
      ctx.globalCompositeOperation = 'lighter';
      for (let i = 0; i < n; i++) {
        const a = c.glow[i] * c.alpha[i] * c.fill[i] * opacity;
        if (a < 0.02) continue;
        const R = c.radius[i] * rk * 2.6 + 2;
        ctx.globalAlpha = Math.min(1, a * GLOW_ALPHA);
        ctx.drawImage(this.glow, xs[i] - R, ys[i] - R, 2 * R, 2 * R);
      }
      ctx.globalCompositeOperation = 'source-over';
    }

    // 2. The dots. A crashed majority throbs.
    for (let i = 0; i < n; i++) {
      const fill = c.fill[i] * (c.crash[i] > 0 ? 1 - c.crash[i] * (1 - throb) : 1);
      const a = c.alpha[i] * fill * opacity;
      if (a < 0.01) continue;
      ctx.globalAlpha = Math.min(1, a);
      ctx.fillStyle = rgb(Math.round(c.red[i]), Math.round(c.green[i]), Math.round(c.blue[i]));
      ctx.beginPath();
      ctx.arc(xs[i], ys[i], c.radius[i] * rk, 0, TWO_PI);
      ctx.fill();
    }

    // 3. Offline: a hollow red ring. 4. Some offline or crashed batteries in a dot: a red edge (it throbs
    // while their controller is down).
    ctx.strokeStyle = OUTAGE;
    for (let i = 0; i < n; i++) {
      const R = c.radius[i] * rk;
      const ring = c.ring[i] * c.alpha[i] * opacity;
      if (ring > 0.02) {
        ctx.globalAlpha = Math.min(1, ring);
        ctx.lineWidth = Math.min(2, Math.max(1.3, R * 0.45));
        ctx.beginPath();
        ctx.arc(xs[i], ys[i], Math.max(0.8, R - ctx.lineWidth / 2), 0, TWO_PI);
        ctx.stroke();
      }
      const edge = c.edge[i];
      if (edge > 0.005) {
        const w = edgeWidth(edge);
        const beat = c.down[i] > 0 ? 1 - Math.min(1, c.down[i] * 6) * (1 - throb) : 1;
        ctx.globalAlpha = Math.min(1, c.alpha[i] * opacity * edgeAlpha(edge) * beat);
        ctx.lineWidth = w;
        ctx.beginPath();
        ctx.arc(xs[i], ys[i], R + w / 2 + 0.3, 0, TWO_PI);
        ctx.stroke();
      }
    }

    // 5. Controller down: a ring spreads out from the dot, like an alarm, until it is handed over.
    if (!this.reduced) {
      ctx.lineWidth = 1.4;
      for (let i = 0; i < n; i++) {
        const k = c.crash[i];
        if (k < 0.05) continue;
        const R = c.radius[i] * rk;
        ctx.globalAlpha = Math.min(1, k * c.alpha[i] * opacity * (1 - pulsePhase) ** 2 * 0.9);
        ctx.beginPath();
        ctx.arc(xs[i], ys[i], R + 1 + 7 * pulsePhase, 0, TWO_PI);
        ctx.stroke();
      }
    }

    // 6. Handed over: the dot flashes once, in its own colour, as its batteries come back.
    if (!this.reduced) {
      for (let i = 0; i < n; i++) {
        const age = now - set.relitStartMs[i];
        if (!(age >= 0 && age < RELIGHT_MS)) continue;
        const p = age / RELIGHT_MS;
        ctx.globalAlpha = Math.min(1, 0.85 * (1 - p) ** 2) * Math.max(0.4, c.alpha[i]) * opacity;
        ctx.strokeStyle = rgb(Math.round(c.red[i]), Math.round(c.green[i]), Math.round(c.blue[i]));
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        ctx.arc(xs[i], ys[i], c.radius[i] * rk + 1 + 5 * p, 0, TWO_PI);
        ctx.stroke();
      }
    }
  }
}

/** Each city's box, from its clearly visible dots (faint outliers would push its label away). */
function computeBoxes(cells: Cells, cityCount: number): CityBox[] {
  const boxes: CityBox[] = [];
  const visible = Math.max(2, cells.refN * 0.1);
  for (let k = 0; k < cityCount; k++) {
    let left = Infinity;
    let top = Infinity;
    let right = -Infinity;
    let bottom = -Infinity;
    let sum = 0;
    let cx = 0;
    let cy = 0;
    for (let c = 0; c < cells.count; c++) {
      if (cells.city[c] !== k || cells.n[c] < visible) continue;
      const r = Math.max(1, cells.spacing * 0.52);
      left = Math.min(left, cells.x[c] - r);
      right = Math.max(right, cells.x[c] + r);
      top = Math.min(top, cells.y[c] - r);
      bottom = Math.max(bottom, cells.y[c] + r);
      sum += cells.n[c];
      cx += cells.x[c] * cells.n[c];
      cy += cells.y[c] * cells.n[c];
    }
    if (sum > 0) boxes.push({ city: k, left, top, right, bottom, cx: cx / sum, cy: cy / sum });
  }
  return boxes;
}
