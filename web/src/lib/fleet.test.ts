import { describe, expect, it } from 'vitest';
import {
  areaOfBattery,
  batteryId,
  batteryIndex,
  batteryRanges,
  controllerFromDetail,
  controllerName,
  DEFAULT_SHARES,
  parseBatteryInput,
} from './fleet.ts';

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

describe('battery ids', () => {
  it('are fleet-wide b00000-b19999', () => {
    expect(batteryId(123)).toBe('b00123');
    expect(batteryIndex('b19999')).toBe(19_999);
    expect(batteryIndex('LZ_HOUSTON-000123')).toBeNull();
  });

  it('accept a number or an id, and reject anything outside the fleet', () => {
    expect(parseBatteryInput('123')).toBe('b00123');
    expect(parseBatteryInput(' b00123 ')).toBe('b00123');
    expect(parseBatteryInput('B42')).toBe('b00042');
    expect(parseBatteryInput('19999')).toBe('b19999');
    expect(parseBatteryInput('20000')).toBeNull();
    expect(parseBatteryInput('-1')).toBeNull();
    expect(parseBatteryInput('LZ_HOUSTON-000123')).toBeNull();
    expect(parseBatteryInput('')).toBeNull();
    expect(parseBatteryInput('150', 100)).toBeNull();
  });
});

describe('batteryRanges (mirrors src/sim/fleet.location_counts)', () => {
  it('splits the default fleet into Houston b00000-b09999 and DFW b10000-b19999', () => {
    const ranges = batteryRanges(20_000, DEFAULT_SHARES);
    expect(ranges).toEqual([
      { location: 'LZ_HOUSTON', first: 0, last: 9_999 },
      { location: 'LZ_NORTH', first: 10_000, last: 19_999 },
    ]);
    expect(areaOfBattery(9_999, ranges)).toBe('LZ_HOUSTON');
    expect(areaOfBattery(10_000, ranges)).toBe('LZ_NORTH');
    expect(areaOfBattery(20_000, ranges)).toBeNull();
  });

  it('gives the remainder to the largest fraction, ties in config order', () => {
    expect(batteryRanges(5, { a: 0.5, b: 0.5 }).map((r) => [r.first, r.last])).toEqual([
      [0, 2],
      [3, 4],
    ]);
    expect(batteryRanges(10, { a: 0.33, b: 0.67 }).map((r) => r.last - r.first + 1)).toEqual([3, 7]);
  });
});
