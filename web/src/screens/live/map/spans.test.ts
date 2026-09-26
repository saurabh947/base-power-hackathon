import { describe, expect, it } from 'vitest';
import { actionSpans } from './spans.ts';

const S0 = Date.parse('2025-05-16T21:00:00Z') / 1000;

describe('actionSpans', () => {
  it('joins consecutive ticks and splits on holds and gaps', () => {
    const xs = [S0, S0 + 300, S0 + 600, S0 + 900, S0 + 1500];
    const actions = ['charge', 'charge', 'hold', 'sell', 'sell'] as const;
    expect(actionSpans(xs, [...actions])).toEqual([
      { start: S0, end: S0 + 600, action: 'charge' },
      { start: S0 + 900, end: S0 + 1200, action: 'sell' },
      { start: S0 + 1500, end: S0 + 1800, action: 'sell' },
    ]);
  });

  it('skips points with no action', () => {
    expect(actionSpans([S0, S0 + 300], [null, 'sell'])).toEqual([{ start: S0 + 300, end: S0 + 600, action: 'sell' }]);
  });
});
