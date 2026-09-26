import { describe, expect, it } from 'vitest';
import { contrastRatio } from '../lib/contrast.ts';
import { OUTLINE_COLOR } from '../map/renderer.ts';
import { PALETTE, type Rgb } from '../map/style.ts';
import mapCss from './map.css?raw';
import tokensCss from './tokens.css?raw';

// AA: 4.5:1 for text, 3:1 for large text and non-text UI (borders, focus rings, chart lines).

function readTokens(block: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of block.matchAll(/--([a-z0-9-]+):\s*([^;]+);/gi)) out[m[1]] = m[2].trim();
  return out;
}

const darkStart = tokensCss.indexOf('@media (prefers-color-scheme: dark)');
const mapStart = tokensCss.indexOf(":root[data-theme='map']");
const light = readTokens(tokensCss.slice(0, darkStart));
const dark = { ...light, ...readTokens(tokensCss.slice(darkStart, mapStart)) };
const mapOwn = readTokens(tokensCss.slice(mapStart));
const map = { ...light, ...mapOwn };

const TEXT_PAIRS: [string, string][] = [
  ['text', 'bg'],
  ['text', 'surface'],
  ['text', 'surface-2'],
  ['text-muted', 'bg'],
  ['text-muted', 'surface'],
  ['text-muted', 'surface-2'],
  ['accent', 'surface'],
  ['accent', 'bg'],
  ['accent-text', 'accent'],
  ['accent', 'accent-soft'],
  ['charge', 'surface'],
  ['sell', 'surface'],
  ['hold', 'surface'],
  ['ok', 'surface'],
  ['warn', 'surface'],
  ['fail', 'surface'],
  ['forecast', 'surface'],
  ['charge', 'charge-soft'],
  ['sell', 'sell-soft'],
  ['hold', 'hold-soft'],
  ['ok', 'ok-soft'],
  ['warn', 'warn-soft'],
  ['fail', 'fail-soft'],
  ['text', 'ok-soft'],
  ['text', 'warn-soft'],
  ['text', 'fail-soft'],
  ['text', 'hold-soft'],
  ['text', 'accent-soft'],
  ['chart-axis', 'surface'],
];

const UI_PAIRS: [string, string][] = [
  ['border-strong', 'surface'],
  ['focus', 'surface'],
  ['focus', 'bg'],
  ['chart-price', 'surface'],
  ['chart-plan', 'surface'],
  ['chart-delivered', 'surface'],
];

describe.each([
  ['light', light],
  ['dark', dark],
  ['live map', map],
])('%s theme contrast', (_name, tokens) => {
  it.each(TEXT_PAIRS)('%s on %s meets 4.5:1', (fg, bg) => {
    expect(tokens[fg]).toBeDefined();
    expect(tokens[bg]).toBeDefined();
    expect(contrastRatio(tokens[fg], tokens[bg])).toBeGreaterThanOrEqual(4.5);
  });

  it.each(UI_PAIRS)('%s against %s meets 3:1', (fg, bg) => {
    expect(contrastRatio(tokens[fg], tokens[bg])).toBeGreaterThanOrEqual(3);
  });
});

const hex = ([r, g, b]: Rgb) => `#${[r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')}`;
const COLOR_TOKENS = Object.keys(light).filter((k) => /^(#|rgb)/.test(light[k]));
const rgbOf = (cls: string) =>
  new RegExp(`\\.${cls}\\s*\\{[^}]*background:\\s*rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)`).exec(mapCss)?.slice(1).map(Number);

describe('the live map', () => {
  it.each(COLOR_TOKENS)('redefines --%s, so nothing light leaks into the always-dark map', (key) => {
    expect(mapOwn[key]).toBeDefined();
  });

  it.each(['text', 'text-muted', 'accent', 'ok', 'warn', 'fail', 'sell', 'charge', 'hold'])('keeps %s readable on its panels', (fg) => {
    expect(contrastRatio(map[fg], map['map-panel-solid'])).toBeGreaterThanOrEqual(4.5);
  });

  it.each(['waiting', 'selling', 'charging', 'offline'] as const)('draws %s dots at 3:1 or better against the dark gray page', (key) => {
    expect(contrastRatio(hex(PALETTE[key]), map.bg)).toBeGreaterThanOrEqual(3);
  });

  it('draws the red outline at 3:1 or better, and selling dots brighter than waiting ones', () => {
    expect(contrastRatio(OUTLINE_COLOR, map.bg)).toBeGreaterThanOrEqual(3);
    expect(map['map-outline']).toBe(OUTLINE_COLOR);
    expect(contrastRatio(hex(PALETTE.selling), map.bg)).toBeGreaterThan(contrastRatio(hex(PALETTE.waiting), map.bg));
  });

  it('uses the same dot colours in the legend as on the canvas', () => {
    expect(rgbOf('code-selling')).toEqual([...PALETTE.selling]);
    expect(rgbOf('code-charging')).toEqual([...PALETTE.charging]);
    expect(rgbOf('code-waiting')).toEqual([...PALETTE.waiting]);
    expect(rgbOf('code-offline')).toEqual([...PALETTE.offline]);
    expect(rgbOf('code-down')).toEqual([...PALETTE.down]);
  });
});

describe('contrastRatio', () => {
  it('matches known values', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5);
    expect(contrastRatio('#ffffff', '#ffffff')).toBeCloseTo(1, 5);
    expect(contrastRatio('#767676', '#ffffff')).toBeGreaterThan(4.5);
  });
});
