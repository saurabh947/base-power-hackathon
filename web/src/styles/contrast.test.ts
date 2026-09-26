import { describe, expect, it } from 'vitest';
import { contrastRatio } from '../lib/contrast.ts';
import tokensCss from './tokens.css?raw';

// AA: 4.5:1 for text, 3:1 for large text and non-text UI (borders, focus rings, chart lines).

function readTokens(block: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of block.matchAll(/--([a-z0-9-]+):\s*([^;]+);/gi)) out[m[1]] = m[2].trim();
  return out;
}

const darkStart = tokensCss.indexOf('@media (prefers-color-scheme: dark)');
const light = readTokens(tokensCss.slice(0, darkStart));
const dark = { ...light, ...readTokens(tokensCss.slice(darkStart)) };

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

describe('contrastRatio', () => {
  it('matches known values', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5);
    expect(contrastRatio('#ffffff', '#ffffff')).toBeCloseTo(1, 5);
    expect(contrastRatio('#767676', '#ffffff')).toBeGreaterThan(4.5);
  });
});
