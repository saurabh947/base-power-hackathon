import { describe, expect, it } from 'vitest';
import type { LiveEvent } from '../api/types.ts';
import { ControllerFlash, RECENT_DOWN_MS } from './flash.ts';
import { DOWN, OFFLINE, SELLING } from './status.ts';

const NOW = Date.parse('2026-09-26T12:00:00Z');

function ev(id: string, kind: string, detail: Record<string, unknown>, agoMs = 500): LiveEvent {
  return { id, wall_utc: new Date(NOW - agoMs).toISOString(), sim_utc: null, kind, severity: 'warn', message: id, detail };
}

/** Newest first, as LiveState.events holds them. */
const newestFirst = (...events: LiveEvent[]) => events.toReversed();

const selling = () => new Uint8Array(20_000).fill(SELLING);
const countDown = (codes: Uint8Array) => codes.reduce((n, c) => n + (c === DOWN ? 1 : 0), 0);

describe('the crash flash when a frame missed code 4', () => {
  it("shows the crashed controller's batteries as down for one frame, from its events", () => {
    const flash = new ControllerFlash();
    flash.reset(20_000, 20, []);
    const codes = selling();
    codes[3_500] = OFFLINE; // stays offline
    const events = newestFirst(
      ev('e1', 'worker_suspect', { worker_id: 3 }),
      ev('e2', 'worker_dead', { worker_id: 3 }),
      ev('e3', 'worker_adopted', { worker_id: 3, adopted_by: [0, 1, 2, 4] }),
    );
    expect(flash.apply(events, codes, NOW)).toBe(999);
    expect(codes[3_000]).toBe(DOWN);
    expect(codes[3_999]).toBe(DOWN);
    expect(codes[3_500]).toBe(OFFLINE);
    expect(codes[2_999]).toBe(SELLING);
    // The next frame: nothing new, nothing marked, so they come back.
    const next = selling();
    expect(flash.apply(events, next, NOW + 1000)).toBe(0);
    expect(countDown(next)).toBe(0);
  });

  it('leaves the frame alone when it already shows code 4, and does not flash the same crash twice', () => {
    const flash = new ControllerFlash();
    flash.reset(20_000, 20, []);
    const shown = selling();
    shown.fill(DOWN, 7_000, 8_000);
    expect(flash.apply(newestFirst(ev('s', 'worker_suspect', { worker_id: 7 })), shown, NOW)).toBe(0);
    // The hand-over arrives a frame later, without code 4: already flashed a moment ago.
    const later = selling();
    const events = newestFirst(ev('s', 'worker_suspect', { worker_id: 7 }), ev('a', 'worker_adopted', { worker_id: 7, adopted_by: [8] }));
    expect(flash.apply(events, later, NOW + 1000)).toBe(0);
    expect(RECENT_DOWN_MS).toBeGreaterThan(1000);
  });

  it("follows hand-overs: a second crash flashes the batteries that controller had adopted too", () => {
    const flash = new ControllerFlash();
    flash.reset(20_000, 20, newestFirst(ev('old', 'worker_adopted', { worker_id: 0, adopted_by: [1] }, 120_000)));
    const codes = selling();
    expect(flash.apply(newestFirst(ev('d1', 'worker_dead', { worker_id: 1 })), codes, NOW)).toBe(2000);
    expect(codes[0]).toBe(DOWN); // was controller 1's since the earlier hand-over
    expect(codes[1_999]).toBe(DOWN);
  });

  it('ignores old events (a reconnect replaying history) and events without a controller', () => {
    const flash = new ControllerFlash();
    flash.reset(20_000, 20, []);
    const codes = selling();
    const events = newestFirst(ev('old', 'worker_dead', { worker_id: 5 }, 5 * 60_000), ev('x', 'worker_dead', {}));
    expect(flash.apply(events, codes, NOW)).toBe(0);
    expect(countDown(codes)).toBe(0);
  });
});
