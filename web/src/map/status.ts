// battery_status (docs/API.md): base64 of packed 4-bit codes. Battery i is in
// byte i >> 1, the low nibble for even i and the high nibble for odd i, so
// 20,000 batteries take 10,000 bytes.

import type { Tick } from '../api/types.ts';
import { base64ToBytes, NO_CITY } from './layout.ts';

export const OFFLINE = 0;
export const WAITING = 1;
export const SELLING = 2;
export const CHARGING = 3;
/** The battery's controller is suspect or dead and it has not been handed over yet. */
export const DOWN = 4;
export const CODES = 5;

/** Codes for `size` batteries into `out` (reused when it fits); null if the field is malformed or short. */
export function decodeStatus(b64: string, size: number, out?: Uint8Array): Uint8Array | null {
  const bytes = base64ToBytes(b64);
  if (!bytes || bytes.length < (size + 1) >> 1) return null;
  const codes = out && out.length === size ? out : new Uint8Array(size);
  for (let i = 0; i < size; i++) {
    const byte = bytes[i >> 1];
    const code = i & 1 ? byte >> 4 : byte & 0x0f;
    // Codes above 4 are not in the contract: treat them as online and waiting.
    codes[i] = code < CODES ? code : WAITING;
  }
  return codes;
}

/** The inverse, for the mock and tests. */
export function packStatus(codes: ArrayLike<number>): Uint8Array {
  const bytes = new Uint8Array((codes.length + 1) >> 1);
  for (let i = 0; i < codes.length; i++) {
    const c = codes[i] & 0x0f;
    bytes[i >> 1] |= i & 1 ? c << 4 : c;
  }
  return bytes;
}

export type CodeCounts = [offline: number, waiting: number, selling: number, charging: number, down: number];

/** How many batteries have each code, for the whole fleet (city = null) or one city index. */
export function countCodes(codes: Uint8Array, cityOf: Uint8Array | null, city: number | null): CodeCounts {
  const out: CodeCounts = [0, 0, 0, 0, 0];
  for (let i = 0; i < codes.length; i++) {
    if (city === null || (cityOf && cityOf[i] === city)) out[codes[i]]++;
  }
  return out;
}

const ACTION_CODE: Record<string, number> = { sell: SELLING, charge: CHARGING, hold: WAITING };

/**
 * A stand-in when a message has no battery_status (an older backend): every battery shows its
 * city's current action. It cannot know which batteries are offline or down, so it never shows them.
 */
export function synthesizeStatus(
  size: number,
  cityOf: Uint8Array,
  locations: readonly string[],
  tick: Tick | null,
  out?: Uint8Array,
): Uint8Array {
  const codes = out && out.length === size ? out : new Uint8Array(size);
  const byCity = locations.map((loc) => ACTION_CODE[tick?.action?.[loc] ?? 'hold'] ?? WAITING);
  for (let i = 0; i < size; i++) codes[i] = cityOf[i] === NO_CITY ? WAITING : (byCity[cityOf[i]] ?? WAITING);
  return codes;
}
