import { describe, expect, it } from 'vitest';
import { batteryRanges, controllerFromDetail, controllerName, DEFAULT_SHARES } from './fleet.ts';

describe('controllers', () => {
  it('counts from 1 for people while worker_id counts from 0', () => {
    expect(controllerName(0)).toBe('Controller 1');
    expect(controllerName(19)).toBe('Controller 20');
  });

  it('reads the controller number from event details', () => {
    expect(controllerFromDetail({ worker_id: 6, controller: 7 })).toBe(7);
    expect(controllerFromDetail({ worker_id: 6 })).toBe(7);
    expect(controllerFromDetail({})).toBeNull();
    expect(controllerFromDetail(undefined)).toBeNull();
  });
});

describe('batteryRanges (mirrors src/sim/fleet.location_counts)', () => {
  it('splits the default fleet into the Texas Triangle blocks from docs/API.md', () => {
    expect(batteryRanges(20_000, DEFAULT_SHARES)).toEqual([
      { location: 'LZ_HOUSTON', first: 0, last: 6_999 },
      { location: 'LZ_NORTH', first: 7_000, last: 14_999 },
      { location: 'LZ_CPS', first: 15_000, last: 17_599 },
      { location: 'LZ_AEN', first: 17_600, last: 19_999 },
    ]);
  });

  it('matches the old two-area split too', () => {
    expect(batteryRanges(20_000, { LZ_HOUSTON: 0.5, LZ_NORTH: 0.5 }).map((r) => r.last)).toEqual([9_999, 19_999]);
  });

  it('gives the remainder to the largest fraction, ties in config order', () => {
    expect(batteryRanges(5, { a: 0.5, b: 0.5 }).map((r) => [r.first, r.last])).toEqual([
      [0, 2],
      [3, 4],
    ]);
    expect(batteryRanges(10, { a: 0.33, b: 0.67 }).map((r) => r.last - r.first + 1)).toEqual([3, 7]);
  });
});
