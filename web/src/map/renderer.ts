// Draws the live map on two stacked canvases: the red Texas outline (redrawn
// only on resize) and the battery dots (redrawn on each 1 Hz frame). Between
// frames nothing is drawn, except while a transition, a controller-down flash
// or a hand-over "re-light" is playing; then a requestAnimationFrame loop runs
// until everything has settled. prefers-reduced-motion: no glows, no pulses,
// no transitions.

import { aggregateCodes, buildCells, dotRadius, nearestCell, spacingFor, type Cells } from './cells.ts';
import { fitBBox, toScreenX, toScreenY, type Padding, type Viewport } from './geometry.ts';
import { NO_CITY, type MapLayout } from './layout.ts';
import { allocTargets, computeTargets, PALETTE, type DotTargets } from './style.ts';
import { TEXAS_BBOX, TEXAS_OUTLINE } from './texasOutline.ts';

export const OUTLINE_COLOR = '#ff3b3b';

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
  /** Last paint, and the slowest of the last 120. */
  paintMs: number;
  maxPaintMs: number;
  /** Last per-frame update: re-counting codes per cell and computing each dot's look. */
  updateMs: number;
  /** Last rebuild of the cells (layout or resize). */
  buildMs: number;
  paints: number;
  animating: boolean;
}

const TAU_LOOK_MS = 160;
const TAU_DARK_RISE_MS = 60;
const TAU_DARK_FALL_MS = 300;
const PULSE_MS = 720;
const POP_MS = 900;
const GLOW_SPRITE = 64;

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

function approach(current: number, target: number, dtMs: number, tauMs: number): number {
  return target + (current - target) * Math.exp(-dtMs / tauMs);
}

export class MapRenderer {
  readonly stats: RenderStats = {
    dots: 0,
    paintMs: 0,
    maxPaintMs: 0,
    updateMs: 0,
    buildMs: 0,
    paints: 0,
    animating: false,
  };

  private readonly base: HTMLCanvasElement;
  private readonly dots: HTMLCanvasElement;
  private readonly glow: HTMLCanvasElement | null;
  private width = 0;
  private height = 0;
  private dpr = 1;
  private padding: Padding = { top: 16, right: 16, bottom: 16, left: 16 };
  private viewport: Viewport | null = null;
  private layout: MapLayout | null = null;
  private cells: Cells | null = null;
  private boxes: CityBox[] = [];
  private codes: Uint8Array | null = null;
  private counts: Uint32Array | null = null;
  private target: DotTargets = allocTargets(0);
  private current: DotTargets = allocTargets(0);
  private popStart = new Float64Array(0);
  private selected: number | null = null;
  private reduced = false;
  private raf = 0;
  private lastStepMs = 0;
  private pulseStartMs = 0;
  private recentPaints: number[] = [];

  constructor(base: HTMLCanvasElement, dots: HTMLCanvasElement) {
    this.base = base;
    this.dots = dots;
    this.glow = makeGlowSprite();
  }

  destroy(): void {
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
  }

  setReducedMotion(on: boolean): void {
    if (on === this.reduced) return;
    this.reduced = on;
    this.paintBase();
    this.update(false);
  }

  setLayout(layout: MapLayout | null): void {
    this.layout = layout;
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

  resize(width: number, height: number, dpr: number, padding: Padding): void {
    const same =
      width === this.width &&
      height === this.height &&
      dpr === this.dpr &&
      JSON.stringify(padding) === JSON.stringify(this.padding);
    if (same) return;
    this.width = width;
    this.height = height;
    this.dpr = dpr;
    this.padding = padding;
    for (const canvas of [this.base, this.dots]) {
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
    }
    this.viewport = fitBBox(TEXAS_BBOX, width, height, padding);
    this.paintBase();
    this.rebuild();
  }

  getViewport(): Viewport | null {
    return this.viewport;
  }

  /** Screen boxes of each city's dots (CSS px), for placing the labels. */
  cityBoxes(): CityBox[] {
    return this.boxes;
  }

  /** The city whose dots are under (x, y), or null. */
  cityAt(x: number, y: number): number | null {
    if (!this.cells) return null;
    const cell = nearestCell(this.cells, x, y, 7);
    if (cell < 0) return null;
    const city = this.cells.city[cell];
    return city === NO_CITY ? null : city;
  }

  // ---- building ----------------------------------------------------------------

  private rebuild(): void {
    const layout = this.layout;
    const vp = this.viewport;
    if (!layout || !vp) {
      this.cells = null;
      this.boxes = [];
      this.stats.dots = 0;
      this.paintDots(performance.now());
      return;
    }
    const t0 = performance.now();
    const sx = new Float32Array(layout.size);
    const sy = new Float32Array(layout.size);
    for (let i = 0; i < layout.size; i++) {
      sx[i] = toScreenX(vp, layout.x[i]);
      sy[i] = toScreenY(vp, layout.y[i]);
    }
    const cells = buildCells(sx, sy, layout.cityOf, spacingFor(vp.height), layout.cities.length);
    this.cells = cells;
    this.counts = null;
    this.target = allocTargets(cells.count);
    this.current = allocTargets(cells.count);
    this.popStart = new Float64Array(cells.count).fill(Number.NaN);
    this.boxes = this.computeBoxes(cells, layout.cities.length);
    this.stats.dots = cells.count;
    this.stats.buildMs = performance.now() - t0;
    this.update(false);
  }

  private computeBoxes(cells: Cells, cityCount: number): CityBox[] {
    const boxes: CityBox[] = [];
    for (let k = 0; k < cityCount; k++) {
      let left = Infinity;
      let top = Infinity;
      let right = -Infinity;
      let bottom = -Infinity;
      let sum = 0;
      let cx = 0;
      let cy = 0;
      for (let c = 0; c < cells.count; c++) {
        if (cells.city[c] !== k) continue;
        const r = Math.max(1, dotRadius(cells.n[c], cells));
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

  // ---- updating ------------------------------------------------------------------

  /** Recompute every dot's target look; animate towards it (or jump, when `animate` is false). */
  private update(animate: boolean): void {
    const cells = this.cells;
    if (!cells) return;
    const t0 = performance.now();
    const codes = this.codes && this.codes.length === cells.cellOf.length ? this.codes : this.defaultCodes(cells);
    this.counts = aggregateCodes(cells, codes, this.counts ?? undefined);
    const prevDark = this.target.dark.slice();
    computeTargets(cells, this.counts, { selected: this.selected, reducedMotion: this.reduced }, this.target);
    const now = performance.now();
    this.stats.updateMs = now - t0;

    if (!animate || this.reduced) {
      for (const key of Object.keys(this.target) as (keyof DotTargets)[]) this.current[key].set(this.target[key]);
      this.popStart.fill(Number.NaN);
      this.paintDots(now);
      return;
    }
    let anyDark = false;
    for (let c = 0; c < cells.count; c++) {
      // Handed over: the dot comes back with a short bright ring.
      if (prevDark[c] > 0.2 && this.target.dark[c] === 0) this.popStart[c] = now;
      if (this.target.dark[c] > 0) anyDark = true;
    }
    if (anyDark && !this.stats.animating) this.pulseStartMs = now;
    this.startLoop();
  }

  private defaultCodes(cells: Cells): Uint8Array {
    // No battery_status yet: every battery online and waiting (what the server sends with no run).
    return new Uint8Array(cells.cellOf.length).fill(1);
  }

  private startLoop(): void {
    if (this.raf) return;
    this.lastStepMs = performance.now();
    this.stats.animating = true;
    const loop = (now: number) => {
      this.raf = 0;
      const dt = Math.min(100, Math.max(0, now - this.lastStepMs));
      this.lastStepMs = now;
      const moving = this.step(dt, now);
      this.paintDots(now);
      if (moving) this.raf = requestAnimationFrame(loop);
      else this.stats.animating = false;
    };
    this.raf = requestAnimationFrame(loop);
  }

  /** Advance every dot towards its target; true while anything still moves, pulses or pops. */
  private step(dtMs: number, now: number): boolean {
    const cells = this.cells;
    if (!cells) return false;
    const t = this.target;
    const c = this.current;
    let moving = false;
    for (let i = 0; i < cells.count; i++) {
      c.red[i] = approach(c.red[i], t.red[i], dtMs, TAU_LOOK_MS);
      c.green[i] = approach(c.green[i], t.green[i], dtMs, TAU_LOOK_MS);
      c.blue[i] = approach(c.blue[i], t.blue[i], dtMs, TAU_LOOK_MS);
      c.alpha[i] = approach(c.alpha[i], t.alpha[i], dtMs, TAU_LOOK_MS);
      c.glow[i] = approach(c.glow[i], t.glow[i], dtMs, TAU_LOOK_MS);
      c.ring[i] = approach(c.ring[i], t.ring[i], dtMs, TAU_LOOK_MS);
      c.radius[i] = approach(c.radius[i], t.radius[i], dtMs, TAU_LOOK_MS);
      c.dark[i] = approach(c.dark[i], t.dark[i], dtMs, t.dark[i] > c.dark[i] ? TAU_DARK_RISE_MS : TAU_DARK_FALL_MS);
      if (
        !moving &&
        (Math.abs(c.red[i] - t.red[i]) > 0.5 ||
          Math.abs(c.green[i] - t.green[i]) > 0.5 ||
          Math.abs(c.blue[i] - t.blue[i]) > 0.5 ||
          Math.abs(c.alpha[i] - t.alpha[i]) > 0.004 ||
          Math.abs(c.glow[i] - t.glow[i]) > 0.004 ||
          Math.abs(c.ring[i] - t.ring[i]) > 0.004 ||
          Math.abs(c.radius[i] - t.radius[i]) > 0.02 ||
          Math.abs(c.dark[i] - t.dark[i]) > 0.004 ||
          t.dark[i] > 0 ||
          now - this.popStart[i] < POP_MS)
      ) {
        moving = true;
      }
    }
    if (!moving) {
      for (const key of Object.keys(t) as (keyof DotTargets)[]) c[key].set(t[key]);
    }
    return moving;
  }

  // ---- painting ------------------------------------------------------------------

  private paintBase(): void {
    const ctx = this.base.getContext('2d');
    const vp = this.viewport;
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
    if (!this.reduced) {
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
    const cells = this.cells;
    if (!cells) return;
    const c = this.current;
    const TWO_PI = Math.PI * 2;

    // Soft glow under selling dots, added light on the dark page.
    if (!this.reduced && this.glow) {
      ctx.globalCompositeOperation = 'lighter';
      for (let i = 0; i < cells.count; i++) {
        const a = c.glow[i] * c.alpha[i] * (1 - c.dark[i]);
        if (a < 0.02) continue;
        const R = c.radius[i] * 3.2 + 3;
        ctx.globalAlpha = Math.min(1, a * 0.5);
        ctx.drawImage(this.glow, cells.x[i] - R, cells.y[i] - R, 2 * R, 2 * R);
      }
      ctx.globalCompositeOperation = 'source-over';
    }

    // The dots.
    for (let i = 0; i < cells.count; i++) {
      if (c.alpha[i] < 0.01) continue;
      ctx.globalAlpha = c.alpha[i];
      ctx.fillStyle = rgb(Math.round(c.red[i]), Math.round(c.green[i]), Math.round(c.blue[i]));
      ctx.beginPath();
      ctx.arc(cells.x[i], cells.y[i], c.radius[i], 0, TWO_PI);
      ctx.fill();
    }

    // A thin light ring around charging dots.
    const [rr, rg, rb] = PALETTE.ring;
    ctx.strokeStyle = rgb(rr, rg, rb);
    ctx.lineWidth = 1;
    for (let i = 0; i < cells.count; i++) {
      const a = c.ring[i] * c.alpha[i] * (1 - c.dark[i]);
      if (a < 0.03 || c.radius[i] < 1.2) continue;
      ctx.globalAlpha = Math.min(1, a * 0.85);
      ctx.beginPath();
      ctx.arc(cells.x[i], cells.y[i], c.radius[i] + 1, 0, TWO_PI);
      ctx.stroke();
    }

    // Controller down: the dot goes dark (pulsing while it waits), then comes back.
    const pulse = this.reduced ? 1 : 0.8 + 0.2 * Math.cos((TWO_PI * (now - this.pulseStartMs)) / PULSE_MS);
    const [dr, dg, db] = PALETTE.down;
    ctx.fillStyle = rgb(dr, dg, db);
    for (let i = 0; i < cells.count; i++) {
      if (c.dark[i] < 0.01) continue;
      ctx.globalAlpha = Math.min(1, c.dark[i] * (this.target.dark[i] > 0 ? pulse : 1)) * Math.max(0.5, c.alpha[i]);
      ctx.beginPath();
      ctx.arc(cells.x[i], cells.y[i], c.radius[i] + 0.5, 0, TWO_PI);
      ctx.fill();
    }

    // Handed over: a bright ring grows out of the dot and fades.
    if (!this.reduced) {
      const [gr, gg, gb] = PALETTE.selling;
      ctx.strokeStyle = rgb(gr, gg, gb);
      ctx.lineWidth = 1.5;
      for (let i = 0; i < cells.count; i++) {
        const age = now - this.popStart[i];
        if (!(age >= 0 && age < POP_MS)) continue;
        const p = age / POP_MS;
        ctx.globalAlpha = 0.9 * (1 - p) * Math.max(0.4, c.alpha[i]);
        ctx.beginPath();
        ctx.arc(cells.x[i], cells.y[i], c.radius[i] + 1 + 7 * (1 - (1 - p) ** 3), 0, TWO_PI);
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;

    const ms = performance.now() - t0;
    this.stats.paintMs = ms;
    this.stats.paints += 1;
    this.recentPaints.push(ms);
    if (this.recentPaints.length > 120) this.recentPaints.shift();
    this.stats.maxPaintMs = Math.max(...this.recentPaints);
  }
}
