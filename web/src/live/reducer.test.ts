import { describe, expect, it } from 'vitest';
import type { FrameMessage, LiveEvent, SnapshotMessage, Tick, Worker } from '../api/types.ts';
import { initialLiveState, liveReducer, MAX_EVENTS, MAX_HISTORY, type LiveState } from './reducer.ts';

const T0 = Date.parse('2025-05-16T21:00:00Z');

function tick(i: number, overrides: Partial<Tick> = {}): Tick {
  const ts = new Date(T0 + i * 300_000).toISOString().replace('.000Z', 'Z');
  return {
    ts_utc: ts,
    ts_local: ts,
    price: { LZ_HOUSTON: 40 + i, LZ_NORTH: 38 + i },
    decision_price: { LZ_HOUSTON: 39, LZ_NORTH: 37 },
    action: { LZ_HOUSTON: 'hold', LZ_NORTH: 'hold' },
    target_kw: { LZ_HOUSTON: 0, LZ_NORTH: 0, fleet: 0 },
    actual_kw: { LZ_HOUSTON: 0, LZ_NORTH: 0, fleet: 0 },
    error_pct: 0,
    revenue_usd: { LZ_HOUSTON: 0, LZ_NORTH: 0, fleet: 0 },
    charge_level: { LZ_HOUSTON: 0.9, LZ_NORTH: 0.9, fleet: 0.9 },
    healthy_workers: 20,
    ...overrides,
  };
}

const workers = (status: Worker['status'] = 'healthy'): Worker[] =>
  Array.from({ length: 20 }, (_, i) => ({
    worker_id: i + 1,
    status,
    pid: 100 + i,
    batteries: 1000,
    last_tick_utc: null,
    adopted_from: [],
  }));

function event(n: number, wallOffsetS = n): LiveEvent {
  return {
    id: `e${n}`,
    wall_utc: new Date(T0 + wallOffsetS * 1000).toISOString(),
    sim_utc: null,
    kind: 'action_changed',
    severity: 'info',
    message: `Event ${n}`,
  };
}

function snapshot(overrides: Partial<SnapshotMessage> = {}): SnapshotMessage {
  return {
    type: 'snapshot',
    run_id: 'run-1',
    status: 'running',
    sim_time_utc: '2025-05-16T21:10:00Z',
    speed_tps: 2,
    settings: {
      preset: 'spike_evening',
      start_utc: '2025-05-16T21:00:00Z',
      speed_tps: 2,
      strategy: 'forecast',
      start_charge_level: 0.9,
      fleet_size: 20000,
      power_kw_range: [5, 11.5],
      capacity_kwh_range: [10, 20],
      locations: { LZ_HOUSTON: 0.5, LZ_NORTH: 0.5 },
    },
    history: [tick(0), tick(1), tick(2)],
    totals: { revenue_usd: 10, sold_kwh: 5, bought_kwh: 1, ticks: 3 },
    workers: workers(),
    failed_commands: { count: 0, recent: [] },
    availability: { available: 20000, total: 20000, by_location: {}, offline_workers: [] },
    events: [event(1), event(2)],
    forecast: {},
    ...overrides,
  };
}

function frame(ticks: Tick[], overrides: Partial<FrameMessage> = {}): FrameMessage {
  return {
    type: 'frame',
    run_id: 'run-1',
    status: 'running',
    sim_time_utc: ticks.length ? ticks[ticks.length - 1].ts_utc : '2025-05-16T21:10:00Z',
    speed_tps: 2,
    ticks,
    totals: { revenue_usd: 12, sold_kwh: 6, bought_kwh: 1, ticks: 3 + ticks.length },
    workers: workers(),
    failed_commands: { count: 0, recent: [] },
    availability: { available: 20000, total: 20000, by_location: {}, offline_workers: [] },
    events: [],
    ...overrides,
  };
}

const apply = (state: LiveState, ...messages: unknown[]) =>
  messages.reduce<LiveState>((s, m) => liveReducer(s, m, 1_000), state);

describe('snapshot', () => {
  it('replaces the view and takes the speed from settings', () => {
    const s = apply(initialLiveState, snapshot());
    expect(s.received).toBe(true);
    expect(s.runId).toBe('run-1');
    expect(s.status).toBe('running');
    expect(s.history).toHaveLength(3);
    expect(s.speedTps).toBe(2);
    expect(s.simTimeUtc).toBe('2025-05-16T21:10:00Z');
    expect(s.events.map((e) => e.id)).toEqual(['e2', 'e1']); // newest first
  });

  it('caps history at MAX_HISTORY and sorts ticks', () => {
    const many = Array.from({ length: MAX_HISTORY + 10 }, (_, i) => tick(i)).toReversed();
    const s = apply(initialLiveState, snapshot({ history: many }));
    expect(s.history).toHaveLength(MAX_HISTORY);
    expect(s.history[0].ts_utc).toBe(tick(10).ts_utc);
    expect(s.history[MAX_HISTORY - 1].ts_utc).toBe(tick(MAX_HISTORY + 9).ts_utc);
  });

  it('keeps ticks newer than a re-fetched snapshot of the same run', () => {
    const s1 = apply(initialLiveState, snapshot(), frame([tick(3), tick(4)]));
    const s2 = apply(s1, snapshot({ history: [tick(0), tick(1), tick(2), tick(3)] }));
    expect(s2.history.map((t) => t.ts_utc)).toEqual([0, 1, 2, 3, 4].map((i) => tick(i).ts_utc));
  });
});

describe('frame', () => {
  it('appends every tick, never duplicates, and caps memory', () => {
    const base = apply(
      initialLiveState,
      snapshot({ history: Array.from({ length: MAX_HISTORY - 1 }, (_, i) => tick(i)) }),
    );
    const s = apply(base, frame([tick(MAX_HISTORY - 2), tick(MAX_HISTORY - 1), tick(MAX_HISTORY), tick(MAX_HISTORY + 1)]));
    expect(s.history).toHaveLength(MAX_HISTORY);
    expect(s.history[s.history.length - 1].ts_utc).toBe(tick(MAX_HISTORY + 1).ts_utc);
    expect(new Set(s.history.map((t) => t.ts_utc)).size).toBe(MAX_HISTORY);
  });

  it('keeps the same object for slices that did not change', () => {
    const s1 = apply(initialLiveState, snapshot());
    const s2 = apply(s1, frame([tick(3)]));
    expect(s2.workers).toBe(s1.workers);
    expect(s2.availability).toBe(s1.availability);
    expect(s2.failedCommands).toBe(s1.failedCommands);
    expect(s2.forecast).toBe(s1.forecast);
    expect(s2.settings).toBe(s1.settings);
    expect(s2.totals).not.toBe(s1.totals);
    expect(s2.history).not.toBe(s1.history);

    const s3 = apply(s2, frame([]));
    expect(s3.history).toBe(s2.history); // a paused frame carries no ticks
  });

  it('adds new events newest first, de-duplicated and capped', () => {
    let s = apply(initialLiveState, snapshot({ events: [] }));
    for (let n = 0; n < MAX_EVENTS + 5; n++) s = apply(s, frame([], { events: [event(n)] }));
    s = apply(s, frame([], { events: [event(MAX_EVENTS + 4)] })); // repeat
    expect(s.events).toHaveLength(MAX_EVENTS);
    expect(s.events[0].id).toBe(`e${MAX_EVENTS + 4}`);
    expect(new Set(s.events.map((e) => e.id)).size).toBe(MAX_EVENTS);
  });

  it('updates the forecast only when the frame carries one', () => {
    const fc = {
      location: 'LZ_HOUSTON',
      series: 'rt_price',
      model: 'timesfm-3.0',
      issued_utc: '2025-05-16T21:00:00Z',
      step_minutes: 15,
      points: [{ ts_utc: '2025-05-16T21:15:00Z', p10: 1, p50: 2, p90: 3 }],
    };
    const s1 = apply(initialLiveState, snapshot(), frame([tick(3)], { forecast: { LZ_HOUSTON: fc } }));
    expect(s1.forecast.LZ_HOUSTON).toEqual(fc);
    const s2 = apply(s1, frame([tick(4)]));
    expect(s2.forecast).toBe(s1.forecast);
  });

  it('starts over, and asks for a resync, when a new run appears', () => {
    const s1 = apply(initialLiveState, snapshot());
    const s2 = apply(s1, frame([tick(0)], { run_id: 'run-2', events: [event(9)] }));
    expect(s2.runId).toBe('run-2');
    expect(s2.history).toHaveLength(1);
    expect(s2.settings).toBeNull();
    expect(s2.events.map((e) => e.id)).toEqual(['e9']);
    expect(s2.runChanged).toBe(s1.runChanged + 1);
  });

  it('tracks speed and status from frames', () => {
    const s = apply(initialLiveState, snapshot(), frame([], { status: 'paused', speed_tps: 10 }));
    expect(s.status).toBe('paused');
    expect(s.speedTps).toBe(10);
  });
});

const idle = (lastRun: unknown = null) => ({ type: 'idle', status: 'idle', run_id: null, last_run: lastRun });

describe('idle and junk', () => {
  it('clears the run on an idle message, keeps the notes and remembers the last run', () => {
    const s1 = apply(
      initialLiveState,
      snapshot({
        workers: workers('dead'),
        availability: {
          available: 17_000,
          total: 20_000,
          by_location: { LZ_HOUSTON: { available: 7_000, total: 10_000 } },
          offline_workers: [3],
        },
      }),
    );
    const totals = { revenue_usd: 163_467.41, sold_kwh: 1, bought_kwh: 2, ticks: 384 };
    const s2 = apply(s1, idle({ run_id: 'run-1', totals }));
    expect(s2.status).toBe('idle');
    expect(s2.runId).toBeNull();
    expect(s2.history).toHaveLength(0);
    expect(s2.totals.revenue_usd).toBe(0);
    expect(s2.events).toHaveLength(2);
    expect(s2.lastRun).toEqual({ runId: 'run-1', totals });
    // Reset restarted the controllers and brought every battery back.
    expect(s2.workers).toHaveLength(0);
    expect(s2.availability).toEqual({
      available: 20_000,
      total: 20_000,
      by_location: { LZ_HOUSTON: { available: 10_000, total: 10_000 } },
      offline_workers: [],
    });
  });

  it('keeps the same objects across repeated idle messages', () => {
    const s1 = apply(initialLiveState, snapshot(), idle({ run_id: 'run-1', totals: { revenue_usd: 1, sold_kwh: 0, bought_kwh: 0, ticks: 1 } }));
    const s2 = apply(s1, idle({ run_id: 'run-1', totals: { revenue_usd: 1, sold_kwh: 0, bought_kwh: 0, ticks: 1 } }));
    expect(s2.lastRun).toBe(s1.lastRun);
    expect(s2.workers).toBe(s1.workers);
    expect(s2.history).toBe(s1.history);
    expect(apply(s2, idle(null)).lastRun).toBeNull();
  });

  it('treats a frame with status idle like an idle message', () => {
    const s = apply(initialLiveState, snapshot(), frame([], { status: 'idle' }));
    expect(s.status).toBe('idle');
    expect(s.history).toHaveLength(0);
  });

  it('ignores anything it does not understand', () => {
    const s1 = apply(initialLiveState, snapshot());
    expect(liveReducer(s1, null)).toBe(s1);
    expect(liveReducer(s1, 'hello')).toBe(s1);
    expect(liveReducer(s1, [1, 2])).toBe(s1);
    expect(liveReducer(s1, { type: 'pong' })).toBe(s1);
    const s2 = apply(s1, frame([{ nonsense: true } as unknown as Tick, tick(3)]));
    expect(s2.history).toHaveLength(4);
  });
});
