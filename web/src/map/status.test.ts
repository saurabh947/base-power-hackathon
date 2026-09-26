import { describe, expect, it } from 'vitest';
import type { Tick } from '../api/types.ts';
import { NO_CITY } from './layout.ts';
import { CHARGING, countCodes, decodeStatus, DOWN, OFFLINE, packStatus, SELLING, synthesizeStatus, WAITING } from './status.ts';

const b64 = (bytes: number[] | Uint8Array) => btoa(String.fromCharCode(...bytes));

describe('battery_status nibbles', () => {
  it('puts battery i in byte i >> 1: the low nibble for even i, the high nibble for odd i', () => {
    // byte 0 = 0x21: battery 0 is 1 (waiting), battery 1 is 2 (selling); byte 1 = 0x43: battery 2 is 3, battery 3 is 4.
    expect(Array.from(decodeStatus(b64([0x21, 0x43]), 4) ?? [])).toEqual([WAITING, SELLING, CHARGING, DOWN]);
    expect(Array.from(decodeStatus(b64([0x10]), 2) ?? [])).toEqual([OFFLINE, WAITING]);
  });

  it('handles an odd fleet size (the last high nibble is unused)', () => {
    expect(Array.from(decodeStatus(b64([0x21, 0xf3]), 3) ?? [])).toEqual([WAITING, SELLING, CHARGING]);
  });

  it('takes 10,000 bytes for 20,000 batteries and round-trips through packStatus', () => {
    const codes = Uint8Array.from({ length: 20_000 }, (_, i) => i % 5);
    const packed = packStatus(codes);
    expect(packed).toHaveLength(10_000);
    const decoded = decodeStatus(btoa(String.fromCharCode(...packed)), 20_000);
    expect(decoded).toEqual(codes);
  });

  it('refuses a field that is too short or not base64, and treats unknown codes as waiting', () => {
    expect(decodeStatus(b64([0x11]), 4)).toBeNull();
    expect(decodeStatus('not base64!', 2)).toBeNull();
    expect(Array.from(decodeStatus(b64([0x9f]), 2) ?? [])).toEqual([WAITING, WAITING]);
  });

  it('reuses the output array when it fits', () => {
    const out = new Uint8Array(2);
    expect(decodeStatus(b64([0x32]), 2, out)).toBe(out);
    expect(Array.from(out)).toEqual([SELLING, CHARGING]);
  });
});

describe('countCodes', () => {
  const codes = Uint8Array.from([1, 2, 2, 0, 4, 3]);
  const cityOf = Uint8Array.from([0, 0, 0, 1, 1, NO_CITY]);

  it('counts the whole fleet or one city', () => {
    expect(countCodes(codes, null, null)).toEqual([1, 1, 2, 1, 1]);
    expect(countCodes(codes, cityOf, 0)).toEqual([0, 1, 2, 0, 0]);
    expect(countCodes(codes, cityOf, 1)).toEqual([1, 0, 0, 0, 1]);
  });
});

describe('synthesizeStatus (no battery_status from the server)', () => {
  it("shows each city's current action and nothing it cannot know", () => {
    const tick = { action: { LZ_A: 'sell', LZ_B: 'charge' } } as unknown as Tick;
    const cityOf = Uint8Array.from([0, 0, 1, NO_CITY]);
    expect(Array.from(synthesizeStatus(4, cityOf, ['LZ_A', 'LZ_B'], tick))).toEqual([SELLING, SELLING, CHARGING, WAITING]);
    expect(Array.from(synthesizeStatus(4, cityOf, ['LZ_A', 'LZ_B'], null))).toEqual([WAITING, WAITING, WAITING, WAITING]);
  });
});
