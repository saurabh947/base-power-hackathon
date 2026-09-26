import { describe, expect, it } from 'vitest';
import { place, placeLabels, type Rect } from './labels.ts';

const box: Rect = { left: 100, top: 100, right: 160, bottom: 140 };
const bounds: Rect = { left: 0, top: 0, right: 1000, bottom: 700 };
const hits = (a: Rect, b: Rect) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

describe('city labels', () => {
  it('sit beside the cluster on the chosen side', () => {
    expect(place(box, 80, 20, 'n', 8)).toEqual({ left: 90, top: 72, right: 170, bottom: 92 });
    expect(place(box, 80, 20, 'e', 8)).toEqual({ left: 168, top: 110, right: 248, bottom: 130 });
    expect(place(box, 80, 20, 'w', 8)).toEqual({ left: 12, top: 110, right: 92, bottom: 130 });
    expect(place(box, 80, 20, 's', 8)).toEqual({ left: 90, top: 148, right: 170, bottom: 168 });
  });

  it('take the preferred side when it is free', () => {
    const spots = placeLabels([{ key: 'LZ_AEN', box, width: 80, height: 20, prefer: ['w'] }], [], bounds);
    expect(spots.get('LZ_AEN')?.right).toBeLessThanOrEqual(box.left);
  });

  it('move away from a corner panel, the screen edge and another label', () => {
    const panel: Rect = { left: 150, top: 90, right: 400, bottom: 200 }; // covers the east side
    const spots = placeLabels([{ key: 'LZ_HOUSTON', box, width: 80, height: 20, prefer: ['e', 'se', 'ne'] }], [panel], bounds);
    const r = spots.get('LZ_HOUSTON');
    expect(r).toBeDefined();
    expect(hits(r as Rect, panel)).toBe(false);

    const edge = placeLabels([{ key: 'x', box: { left: 2, top: 300, right: 40, bottom: 330 }, width: 90, height: 20, prefer: ['w'] }], [], bounds);
    expect(edge.get('x')?.left).toBeGreaterThanOrEqual(0);

    const two = placeLabels(
      [
        { key: 'a', box, width: 80, height: 20, prefer: ['e'] },
        { key: 'b', box: { left: 100, top: 145, right: 160, bottom: 175 }, width: 80, height: 20, prefer: ['e', 'se'] },
      ],
      [],
      bounds,
    );
    const a = two.get('a') as Rect;
    const b = two.get('b') as Rect;
    expect(hits(a, b)).toBe(false);
  });
});
