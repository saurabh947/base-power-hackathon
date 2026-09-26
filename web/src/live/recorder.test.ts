import { describe, expect, it } from 'vitest';
import type { Tick, Worker } from '../api/types.ts';
import { packStatus } from '../map/status.ts';
import { FrameRecorder, RECORDER_MAX_BYTES } from './recorder.ts';
import { initialLiveState, type LiveState } from './reducer.ts';

const T0 = Date.parse('2025-07-11T23:00:00Z');
const tick = (i: number) => ({ ts_utc: new Date(T0 + i * 300_000).toISOString().replace('.000Z', 'Z') }) as Tick;

/** battery_status for 20 batteries, all with `code`. */
const status = (code: number) => btoa(String.fromCharCode(...packStatus(Array.from({ length: 20 }, () => code))));

/** Controller 7 with this usable charge (null: crashed and handed over). */
const workers = (usable: number | null): Worker[] => [
  {
    worker_id: 6,
    status: usable === null ? 'dead' : 'healthy',
    pid: 7,
    batteries: usable === null ? 0 : 1000,
    last_tick_utc: null,
    adopted_from: [],
    usable_charge: usable,
    charge_level: usable === null ? null : 0.2 + usable * 0.8,
  },
];

function state(i: number, code = 1, runId = 'run-1', status_: LiveState['status'] = 'running'): LiveState {
  return {
    ...initialLiveState,
    received: true,
    runId,
    status: status_,
    history: Array.from({ length: i + 1 }, (_, k) => tick(k)),
    batteryStatus: status(code),
  };
}

describe('the frames this page has seen', () => {
  it('keeps each frame’s battery_status, controllers, availability and race by its latest tick', () => {
    const r = new FrameRecorder();
    expect(r.add(state(0, 1))).toBe(true);
    expect(r.add(state(3, 2))).toBe(true);
    expect(r.records.map((x) => x.tMs)).toEqual([T0, T0 + 3 * 300_000]);
    expect(r.at(T0 + 3 * 300_000)?.status?.[0]).toBe(0x22);
    // Between frames, the latest frame at or before the moment.
    expect(r.at(T0 + 2 * 300_000)?.tMs).toBe(T0);
    // Before this page was opened: nothing.
    expect(r.at(T0 - 1)).toBeNull();
    expect(r.firstMs()).toBe(T0);
  });

  it('keeps each frame’s controllers with their charge, so the charge gauges can look back', () => {
    const r = new FrameRecorder();
    r.add({ ...state(1), workers: workers(0.5) });
    r.add({ ...state(2), workers: workers(0) }); // crashed: its batteries emptied
    r.add({ ...state(3), workers: workers(null) }); // handed over
    expect(r.at(T0 + 300_000)?.workers[0].usable_charge).toBe(0.5);
    expect(r.at(T0 + 2 * 300_000)?.workers[0].usable_charge).toBe(0);
    expect(r.at(T0 + 3 * 300_000)?.workers[0]).toMatchObject({ status: 'dead', usable_charge: null });
  });

  it('replaces the view of the same moment (paused), sharing unchanged bytes', () => {
    const r = new FrameRecorder();
    r.add(state(2, 1));
    r.add(state(2, 1));
    expect(r.records).toHaveLength(1);
    expect(r.sizeBytes).toBe(10);
    r.add(state(2, 0)); // taken offline while paused
    expect(r.records).toHaveLength(1);
    expect(r.at(T0 + 2 * 300_000)?.status?.[0]).toBe(0x00);
    expect(r.sizeBytes).toBe(10);
    r.add(state(3, 0)); // the same bytes on the next frame: shared, not copied
    expect(r.records[1].status).toBe(r.records[0].status);
    expect(r.sizeBytes).toBe(10);
  });

  it('starts over with a new run, and records nothing while idle or starting', () => {
    const r = new FrameRecorder();
    r.add(state(2));
    expect(r.add(state(1, 1, 'run-2'))).toBe(true);
    expect(r.records).toHaveLength(1);
    expect(r.records[0].tMs).toBe(T0 + 300_000);
    expect(r.add({ ...state(4, 1, 'run-2'), status: 'starting' })).toBe(false);
    expect(r.add({ ...initialLiveState, received: true, status: 'idle' })).toBe(false);
    expect(r.records).toHaveLength(0);
  });

  it('marks a gap when the page stops listening: nothing is known until the next frame', () => {
    const r = new FrameRecorder();
    r.add(state(1));
    r.gap();
    r.add(state(6));
    expect(r.at(T0 + 300_000)?.tMs).toBe(T0 + 300_000);
    expect(r.at(T0 + 3 * 300_000)).toBeNull(); // in the gap
    expect(r.at(T0 + 6 * 300_000)?.tMs).toBe(T0 + 6 * 300_000);
  });

  it('stays under its memory cap by thinning the older half, so the whole visit stays scrubbable', () => {
    const r = new FrameRecorder(100); // 10 frames of 10 bytes
    for (let i = 0; i < 40; i++) r.add(state(i, i % 2 ? 2 : 3));
    expect(r.sizeBytes).toBeLessThanOrEqual(100);
    expect(r.records[0].tMs).toBe(T0); // the start is kept
    expect(r.records.at(-1)?.tMs).toBe(T0 + 39 * 300_000); // and the latest
    // Every moment still finds a frame at or before it.
    for (let i = 0; i < 40; i++) expect(r.at(T0 + i * 300_000)).not.toBeNull();
    expect(RECORDER_MAX_BYTES).toBe(8 * 1024 * 1024);
  });
});
