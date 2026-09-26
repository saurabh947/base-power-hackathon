import { describe, expect, it } from 'vitest';
import indexHtml from '../../index.html?raw';
import { contrastRatio } from '../lib/contrast.ts';
import { OUTLINE_COLOR } from '../map/renderer.ts';
import { PALETTE, type Rgb } from '../map/style.ts';
import appCss from './app.css?raw';
import mapCss from './map.css?raw';
import tokensCss from './tokens.css?raw';

// AA: 4.5:1 for text, 3:1 for large text and non-text UI (borders, focus rings, bars, chart lines).
// The whole site has one theme, the live map's (docs/UX.md, Live map): always dark.

function readTokens(css: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of css.matchAll(/--([a-z0-9-]+):\s*([^;]+);/gi)) out[m[1]] = m[2].trim();
  return out;
}

const tokens = readTokens(tokensCss);

/** What text on a translucent panel sits on: the panel over the page. */
const PANEL = 'panel-solid';

const TEXT_PAIRS: [string, string][] = [
  // Body text and secondary text, on the page, on panels and on raised surfaces.
  ['text', 'bg'],
  ['text', 'surface'],
  ['text', 'surface-2'],
  ['text', PANEL],
  ['text-muted', 'bg'],
  ['text-muted', 'surface'],
  ['text-muted', 'surface-2'],
  ['text-muted', PANEL],
  // Links, quiet buttons and "How it works" in green; hovered too.
  ['accent', 'bg'],
  ['accent', 'surface'],
  ['accent', 'surface-2'],
  ['accent', PANEL],
  ['accent-hover', 'bg'],
  ['accent-hover', PANEL],
  // The primary action (Start the simulation, Watch live, Resume): dark text on green.
  ['accent-text', 'accent'],
  ['accent-text', 'accent-hover'],
  // The current page in the nav, a checked segment or setting.
  ['accent', 'accent-soft'],
  ['text', 'accent-soft'],
  ['text-muted', 'accent-soft'],
  // Status colours as text.
  ['charge', 'surface'],
  ['sell', 'surface'],
  ['hold', 'surface'],
  ['ok', 'surface'],
  ['warn', 'surface'],
  ['fail', 'surface'],
  ['forecast', 'surface'],
  ['charge', PANEL],
  ['sell', PANEL],
  ['hold', PANEL],
  ['ok', PANEL],
  ['warn', PANEL],
  ['fail', PANEL],
  ['charge', 'charge-soft'],
  ['sell', 'sell-soft'],
  ['hold', 'hold-soft'],
  ['ok', 'ok-soft'],
  ['warn', 'warn-soft'],
  ['fail', 'fail-soft'],
  // Text inside tinted notices, tiles and the practice banner.
  ['text', 'ok-soft'],
  ['text', 'warn-soft'],
  ['text', 'fail-soft'],
  ['text', 'hold-soft'],
  ['text-muted', 'hold-soft'],
  ['text-muted', 'warn-soft'],
  ['text-muted', 'fail-soft'],
  ['chart-axis', 'surface'],
  ['chart-axis', PANEL],
  // Results: the big per-battery number, the headline bar's value; Start: "20,000 home batteries".
  ['money', PANEL],
  ['money', 'bg'],
  // Results, "What the AI adds": text and the AI's green inside the cards, the percentage chip.
  ['text', 'card'],
  ['text-muted', 'card'],
  ['money', 'card'],
  ['ok', 'ok-soft'],
];

const UI_PAIRS: [string, string][] = [
  // Outlined buttons, inputs and the switch.
  ['border-strong', 'surface'],
  ['border-strong', 'bg'],
  ['border-strong', PANEL],
  // The keyboard focus ring, wherever it lands.
  ['focus', 'surface'],
  ['focus', 'bg'],
  ['focus', PANEL],
  ['focus', 'surface-2'],
  // A primary button stands out from the page and from a panel.
  ['accent', 'bg'],
  ['accent', PANEL],
  // Info notices and events.
  ['info', 'bg'],
  ['info', PANEL],
  // Detailed charts.
  ['chart-price', 'surface'],
  ['chart-plan', 'surface'],
  ['chart-delivered', 'surface'],
  ['chart-price', PANEL],
  ['chart-plan', PANEL],
  ['chart-delivered', PANEL],
  // Results bars against their track, and the day and city bars on the panel.
  ['bar-headline', 'bar-track'],
  ['bar-other', 'bar-track'],
  ['bar-ceiling', 'bar-track'],
  ['bar-day', 'bar-track'],
  ['bar-day', PANEL],
  ['bar-headline', PANEL],
  // "What the AI adds": the AI's bar and the battery gauges' outline inside a card.
  ['bar-headline', 'card'],
  ['border-strong', 'card'],
  // The red Texas outline on the page.
  ['map-outline', 'bg'],
];

describe('the site theme', () => {
  it.each(TEXT_PAIRS)('%s on %s meets 4.5:1', (fg, bg) => {
    expect(tokens[fg]).toBeDefined();
    expect(tokens[bg]).toBeDefined();
    expect(contrastRatio(tokens[fg], tokens[bg])).toBeGreaterThanOrEqual(4.5);
  });

  it.each(UI_PAIRS)('%s against %s meets 3:1', (fg, bg) => {
    expect(tokens[fg]).toBeDefined();
    expect(tokens[bg]).toBeDefined();
    expect(contrastRatio(tokens[fg], tokens[bg])).toBeGreaterThanOrEqual(3);
  });

  it('keeps the gray bars apart from the green headline bar and the page gray (#25282c)', () => {
    expect(tokens.bg).toBe('#25282c');
    expect(contrastRatio(tokens['bar-headline'], tokens['bar-other'])).toBeGreaterThan(1.5);
  });
});

describe('always dark', () => {
  it('has one set of tokens, whatever prefers-color-scheme says', () => {
    for (const css of [tokensCss, appCss, mapCss]) {
      expect(css).not.toMatch(/@media[^{]*prefers-color-scheme/);
      expect(css).not.toMatch(/\[data-theme/);
    }
    expect(tokensCss.match(/^:root\s*\{/gm)).toHaveLength(1);
    expect(tokensCss).toMatch(/^:root\s*\{\s*color-scheme:\s*dark;/m);
  });

  it('tells the browser before any CSS loads (no white flash, dark form controls)', () => {
    expect(indexHtml).toMatch(/<meta name="color-scheme" content="dark"/);
    expect(indexHtml).toMatch(new RegExp(`<meta name="theme-color" content="${tokens.bg}"`));
  });

  it('defines every colour token the stylesheets use', () => {
    // --map-left/top/width/height are set by the map at run time and always come with a fallback.
    const used = new Set([...`${appCss}\n${mapCss}`.matchAll(/var\(--([a-z0-9-]+)\s*\)/gi)].map((m) => m[1]));
    for (const name of used) expect(tokens[name], `--${name}`).toBeDefined();
  });

  it('uses only hex colours for the tokens that text and marks sit on or are drawn in', () => {
    for (const [fg, bg] of [...TEXT_PAIRS, ...UI_PAIRS]) {
      expect(tokens[fg]).toMatch(/^#[0-9a-f]{6}$/i);
      expect(tokens[bg]).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });
});

const hex = ([r, g, b]: Rgb) => `#${[r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')}`;
const rgbOf = (cls: string) =>
  new RegExp(`\\.${cls}\\s*\\{[^}]*background:\\s*rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)`).exec(mapCss)?.slice(1).map(Number);

describe('the live map', () => {
  it.each(['text', 'text-muted', 'accent', 'ok', 'warn', 'fail', 'sell', 'charge', 'hold', 'info'])('keeps %s readable on its panels', (fg) => {
    expect(contrastRatio(tokens[fg], tokens[PANEL])).toBeGreaterThanOrEqual(4.5);
  });

  it.each(['waiting', 'selling', 'charging', 'offline'] as const)('draws %s dots at 3:1 or better against the dark gray page', (key) => {
    expect(contrastRatio(hex(PALETTE[key]), tokens.bg)).toBeGreaterThanOrEqual(3);
  });

  it('draws the red outline at 3:1 or better, and selling dots brighter than waiting ones', () => {
    expect(contrastRatio(OUTLINE_COLOR, tokens.bg)).toBeGreaterThanOrEqual(3);
    expect(tokens['map-outline']).toBe(OUTLINE_COLOR);
    expect(contrastRatio(hex(PALETTE.selling), tokens.bg)).toBeGreaterThan(contrastRatio(hex(PALETTE.waiting), tokens.bg));
  });

  it('uses the same dot colours in the legend as on the canvas', () => {
    expect(rgbOf('code-selling')).toEqual([...PALETTE.selling]);
    expect(rgbOf('code-charging')).toEqual([...PALETTE.charging]);
    expect(rgbOf('code-waiting')).toEqual([...PALETTE.waiting]);
    expect(rgbOf('code-offline')).toEqual([...PALETTE.offline]);
    expect(rgbOf('code-down')).toEqual([...PALETTE.down]);
  });

  it('shows the selling green as the big results number', () => {
    expect(tokens.money.toLowerCase()).toBe(hex(PALETTE.selling));
  });
});

describe('contrastRatio', () => {
  it('matches known values', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5);
    expect(contrastRatio('#ffffff', '#ffffff')).toBeCloseTo(1, 5);
    expect(contrastRatio('#767676', '#ffffff')).toBeGreaterThan(4.5);
  });
});
