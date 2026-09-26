import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { asSnapshot, backoffDelay, DEFAULT_BACKOFF, LiveConnection, type ConnectionState, type SocketLike } from './connection.ts';

class FakeSocket implements SocketLike {
  onopen: SocketLike['onopen'] = null;
  onmessage: SocketLike['onmessage'] = null;
  onclose: SocketLike['onclose'] = null;
  onerror: SocketLike['onerror'] = null;
  closed = false;
  readonly url: string;
  constructor(url: string) {
    this.url = url;
  }
  close() {
    this.closed = true;
  }
  open() {
    this.onopen?.({});
  }
  send(message: unknown) {
    this.onmessage?.({ data: typeof message === 'string' ? message : JSON.stringify(message) });
  }
  drop(code = 1006) {
    this.onclose?.({ code });
  }
}

function setup(overrides: Partial<ConstructorParameters<typeof LiveConnection>[0]> = {}) {
  const sockets: FakeSocket[] = [];
  const messages: unknown[] = [];
  const states: ConnectionState[] = [];
  const fetchState = vi.fn<(signal: AbortSignal) => Promise<unknown>>(async () => ({ run_id: 'r', status: 'running', history: [] }));
  const conn = new LiveConnection({
    url: 'wss://example.test/live',
    fetchState,
    onMessage: (m) => messages.push(m),
    onState: (s) => states.push(s),
    createSocket: (url) => {
      const s = new FakeSocket(url);
      sockets.push(s);
      return s;
    },
    random: () => 0.5, // no jitter: delays are exactly 500 ms, 1 s, 2 s …
    ...overrides,
  });
  const last = () => sockets[sockets.length - 1];
  return { conn, sockets, messages, states, fetchState, last };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-09-25T12:00:00Z'));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('backoffDelay', () => {
  it('doubles from 500 ms and caps at 15 s', () => {
    const delays = [1, 2, 3, 4, 5, 6, 7].map((n) => backoffDelay(n, DEFAULT_BACKOFF, () => 0.5));
    expect(delays).toEqual([500, 1000, 2000, 4000, 8000, 15000, 15000]);
  });

  it('spreads retries by ±25% so visitors do not reconnect in lockstep', () => {
    expect(backoffDelay(3, DEFAULT_BACKOFF, () => 0)).toBe(1500);
    expect(backoffDelay(3, DEFAULT_BACKOFF, () => 0.999999)).toBe(2500);
    expect(backoffDelay(10, DEFAULT_BACKOFF, () => 0.999999)).toBe(15000);
    expect(backoffDelay(0, DEFAULT_BACKOFF, () => 0.5)).toBe(500);
  });
});

describe('LiveConnection', () => {
  it('connects, parses messages and ignores garbage', () => {
    const { conn, last, messages, states } = setup();
    conn.start();
    expect(states.at(-1)?.mode).toBe('connecting');
    last().open();
    expect(conn.state.mode).toBe('live');
    last().send({ type: 'snapshot', run_id: 'r' });
    last().send('not json {');
    last().send({ type: 'frame' });
    expect(messages).toEqual([{ type: 'snapshot', run_id: 'r' }, { type: 'frame' }]);
    conn.stop();
  });

  it('retries a socket that never opens with growing delays', () => {
    const { conn, sockets, last } = setup({ failuresBeforePolling: 10 });
    conn.start();
    last().drop();
    expect(conn.state.mode).toBe('reconnecting');
    expect(conn.state.nextRetryAtMs).toBe(Date.now() + 500);
    vi.advanceTimersByTime(499);
    expect(sockets).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(2);
    last().drop();
    vi.advanceTimersByTime(999);
    expect(sockets).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(sockets).toHaveLength(3);
    conn.stop();
  });

  it('reconnects quickly after a long-lived connection closes (Cloud Run 60-minute limit)', () => {
    const { conn, sockets, last } = setup({ staleAfterMs: 2 * 60 * 60 * 1000 });
    conn.start();
    last().open();
    vi.advanceTimersByTime(60 * 60 * 1000 - 1);
    last().send({ type: 'frame' });
    expect(sockets).toHaveLength(1);
    last().drop(1001);
    expect(conn.state.mode).toBe('reconnecting');
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(2);
    last().open();
    expect(conn.state.mode).toBe('live');
    conn.stop();
  });

  it('backs off when a socket keeps dropping right after it opens', () => {
    const { conn, sockets, last } = setup();
    conn.start();
    last().open();
    last().drop();
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(2);
    last().open();
    last().drop();
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(2); // second quick drop waits 1 s
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(3);
    conn.stop();
  });

  it('closes a quiet socket and reconnects (watchdog)', () => {
    const { conn, sockets, last } = setup({ staleAfterMs: 10_000 });
    conn.start();
    const first = last();
    first.open();
    vi.advanceTimersByTime(9_000);
    first.send({ type: 'frame' });
    vi.advanceTimersByTime(9_999);
    expect(first.closed).toBe(false);
    vi.advanceTimersByTime(1);
    expect(first.closed).toBe(true);
    expect(conn.state.mode).toBe('reconnecting');
    vi.advanceTimersByTime(1000);
    expect(sockets).toHaveLength(2);
    conn.stop();
  });

  it('falls back to polling GET /state, then returns to the WebSocket when it works again', async () => {
    const { conn, sockets, last, messages, fetchState } = setup({
      failuresBeforePolling: 3,
      pollIntervalMs: 2_000,
      socketRetryWhilePollingMs: 30_000,
    });
    conn.start();
    last().drop(); // 1st failure
    await vi.advanceTimersByTimeAsync(500);
    last().drop(); // 2nd
    await vi.advanceTimersByTimeAsync(1000);
    last().drop(); // 3rd: poll instead
    expect(conn.state.mode).toBe('polling');
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchState).toHaveBeenCalledTimes(1);
    expect(messages.at(-1)).toMatchObject({ type: 'snapshot', run_id: 'r' });

    await vi.advanceTimersByTimeAsync(2_000);
    expect(fetchState).toHaveBeenCalledTimes(2);
    expect(sockets).toHaveLength(3);

    await vi.advanceTimersByTimeAsync(28_000); // background WebSocket retry
    expect(sockets).toHaveLength(4);
    last().open();
    expect(conn.state.mode).toBe('live');
    const polls = fetchState.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(fetchState.mock.calls.length).toBe(polls);
    conn.stop();
  });

  it('reports offline when polling fails too, and keeps trying', async () => {
    const fetchState = vi.fn<(signal: AbortSignal) => Promise<unknown>>(async () => {
      throw new Error('network down');
    });
    const { conn, last } = setup({ fetchState, failuresBeforePolling: 1, pollIntervalMs: 2_000 });
    conn.start();
    last().drop();
    await vi.advanceTimersByTimeAsync(0);
    expect(conn.state.mode).toBe('offline');
    expect(conn.state.lastError).toBe('network down');
    await vi.advanceTimersByTimeAsync(2_000);
    expect(fetchState).toHaveBeenCalledTimes(2);
    conn.stop();
  });

  it('stops for good', () => {
    const { conn, sockets, last } = setup();
    conn.start();
    last().open();
    conn.stop();
    expect(last().closed).toBe(true);
    expect(conn.state.mode).toBe('idle');
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
  });

  it('treats a missing WebSocket like a failed attempt', () => {
    const { conn } = setup({
      createSocket: () => {
        throw new Error('WebSocket is not available');
      },
      failuresBeforePolling: 1,
    });
    conn.start();
    expect(conn.isPolling).toBe(true);
    conn.stop();
  });

  it('tags GET /state bodies as snapshots', () => {
    expect(asSnapshot({ run_id: 'x' })).toEqual({ run_id: 'x', type: 'snapshot' });
    expect(asSnapshot({ type: 'snapshot' })).toEqual({ type: 'snapshot' });
    expect(asSnapshot(null)).toBeNull();
  });
});
