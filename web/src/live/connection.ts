// The live feed: WebSocket /live (docs/API.md) with automatic reconnects.
//
// - Reconnects with exponential backoff and jitter. A connection that was open
//   and then closed (Cloud Run closes WebSockets after 60 minutes) reconnects
//   almost at once; the server sends a fresh snapshot on every connect.
// - A watchdog closes a socket that goes quiet: the server sends something
//   every second, even when idle.
// - If the WebSocket cannot open a few times in a row (a proxy or network that
//   blocks WebSockets), it falls back to polling GET /state and keeps retrying
//   the WebSocket in the background.
//
// Timers come from the global scope so tests can drive them with fake timers.

export interface BackoffOptions {
  baseMs: number;
  maxMs: number;
  factor: number;
  /** 0.25 spreads each delay over ±25% so many visitors do not retry in lockstep. */
  jitter: number;
}

export const DEFAULT_BACKOFF: BackoffOptions = { baseMs: 500, maxMs: 15_000, factor: 2, jitter: 0.25 };

/** Delay before retry number `attempt` (1-based): 500 ms, 1 s, 2 s, 4 s … capped at maxMs. */
export function backoffDelay(attempt: number, options: BackoffOptions = DEFAULT_BACKOFF, random: () => number = Math.random): number {
  const n = Math.max(1, Math.floor(attempt));
  const raw = Math.min(options.maxMs, options.baseMs * options.factor ** (n - 1));
  const spread = raw * options.jitter;
  const jittered = raw - spread + 2 * spread * random();
  return Math.round(Math.min(options.maxMs, Math.max(0, jittered)));
}

export interface SocketLike {
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code?: number; reason?: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  close(code?: number, reason?: string): void;
}

export type ConnectionMode = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'polling' | 'offline';

export interface ConnectionState {
  mode: ConnectionMode;
  /** Consecutive failed attempts since the last good connection. */
  attempt: number;
  /** When the next retry happens (ms since epoch), if one is scheduled. */
  nextRetryAtMs: number | null;
  lastError: string | null;
  transport: 'websocket' | 'polling' | null;
}

export const initialConnectionState: ConnectionState = {
  mode: 'idle',
  attempt: 0,
  nextRetryAtMs: null,
  lastError: null,
  transport: null,
};

export interface LiveConnectionOptions {
  url: string;
  /** GET /state; resolves to the snapshot object. */
  fetchState: (signal: AbortSignal) => Promise<unknown>;
  onMessage: (message: unknown) => void;
  onState?: (state: ConnectionState) => void;
  createSocket?: (url: string) => SocketLike;
  now?: () => number;
  random?: () => number;
  backoff?: BackoffOptions;
  /** WebSocket attempts that never open before polling starts. */
  failuresBeforePolling?: number;
  pollIntervalMs?: number;
  /** While polling, how often to try the WebSocket again. */
  socketRetryWhilePollingMs?: number;
  /** Close a socket that has sent nothing for this long. */
  staleAfterMs?: number;
}

type Timer = ReturnType<typeof setTimeout>;

/** A connection that stayed open this long counts as healthy when it closes. */
const MIN_HEALTHY_MS = 10_000;

function defaultCreateSocket(url: string): SocketLike {
  if (typeof WebSocket === 'undefined') throw new Error('WebSocket is not available');
  return new WebSocket(url) as unknown as SocketLike;
}

/** GET /state returns the snapshot object; make sure it is tagged as one. */
export function asSnapshot(value: unknown): unknown {
  if (value && typeof value === 'object' && !Array.isArray(value) && !('type' in value)) {
    return { ...(value as Record<string, unknown>), type: 'snapshot' };
  }
  return value;
}

export class LiveConnection {
  state: ConnectionState = { ...initialConnectionState };

  private readonly url: string;
  private readonly fetchState: (signal: AbortSignal) => Promise<unknown>;
  private readonly onMessage: (message: unknown) => void;
  private readonly onState: (state: ConnectionState) => void;
  private readonly createSocket: (url: string) => SocketLike;
  private readonly now: () => number;
  private readonly random: () => number;
  private readonly backoff: BackoffOptions;
  private readonly failuresBeforePolling: number;
  private readonly pollIntervalMs: number;
  private readonly socketRetryWhilePollingMs: number;
  private readonly staleAfterMs: number;

  private running = false;
  private socket: SocketLike | null = null;
  private socketOpen = false;
  private openedAtMs = 0;
  private everConnected = false;
  private failures = 0;
  private attempt = 0;
  private polling = false;
  private pollFailures = 0;
  private pollAbort: AbortController | null = null;
  private retryTimer: Timer | null = null;
  private pollTimer: Timer | null = null;
  private staleTimer: Timer | null = null;

  constructor(options: LiveConnectionOptions) {
    this.url = options.url;
    this.fetchState = options.fetchState;
    this.onMessage = options.onMessage;
    this.onState = options.onState ?? (() => {});
    this.createSocket = options.createSocket ?? defaultCreateSocket;
    this.now = options.now ?? Date.now;
    this.random = options.random ?? Math.random;
    this.backoff = options.backoff ?? DEFAULT_BACKOFF;
    this.failuresBeforePolling = options.failuresBeforePolling ?? 3;
    this.pollIntervalMs = options.pollIntervalMs ?? 2_000;
    this.socketRetryWhilePollingMs = options.socketRetryWhilePollingMs ?? 30_000;
    this.staleAfterMs = options.staleAfterMs ?? 10_000;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.openSocket();
  }

  stop(): void {
    this.running = false;
    this.clearRetry();
    this.clearStale();
    this.stopPolling();
    this.dropSocket();
    this.setState({ ...initialConnectionState });
  }

  /** Try right away (a "Try again" button, or the browser coming back online). */
  retryNow(): void {
    if (!this.running || this.socket) return;
    this.clearRetry();
    this.openSocket();
  }

  get isPolling(): boolean {
    return this.polling;
  }

  private setState(patch: Partial<ConnectionState>): void {
    this.state = { ...this.state, ...patch };
    this.onState(this.state);
  }

  private openSocket(): void {
    this.clearRetry();
    let socket: SocketLike;
    try {
      socket = this.createSocket(this.url);
    } catch (err) {
      this.handleEnd(null, err instanceof Error ? err.message : 'WebSocket unavailable');
      return;
    }
    this.socket = socket;
    this.socketOpen = false;
    if (!this.polling) {
      this.setState({
        mode: this.everConnected || this.attempt > 0 ? 'reconnecting' : 'connecting',
        nextRetryAtMs: null,
      });
    }

    let ended = false;
    const end = (reason: string) => {
      if (ended || this.socket !== socket) return;
      ended = true;
      this.handleEnd(socket, reason);
    };

    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.socketOpen = true;
      this.openedAtMs = this.now();
      this.everConnected = true;
      this.failures = 0;
      this.stopPolling();
      this.setState({ mode: 'live', transport: 'websocket', attempt: 0, nextRetryAtMs: null, lastError: null });
      this.armStale();
    };
    socket.onmessage = (ev) => {
      if (this.socket !== socket) return;
      this.armStale();
      if (typeof ev.data !== 'string') return;
      let message: unknown;
      try {
        message = JSON.parse(ev.data);
      } catch {
        return;
      }
      this.onMessage(message);
    };
    socket.onclose = (ev) => end(ev && ev.code ? `connection closed (code ${ev.code})` : 'connection closed');
    socket.onerror = () => end('connection error');
  }

  private handleEnd(socket: SocketLike | null, reason: string): void {
    const wasOpen = socket !== null && this.socketOpen;
    this.clearStale();
    this.dropSocket();
    if (!this.running) return;

    if (wasOpen) {
      // A healthy connection ended (for example Cloud Run's 60-minute limit): come back quickly.
      // One that dropped within seconds of opening backs off like a failure, so a server that
      // accepts and then hangs up is not hammered.
      const lived = this.now() - this.openedAtMs;
      this.attempt = lived >= MIN_HEALTHY_MS ? 1 : this.attempt + 1;
    } else {
      this.failures += 1;
      this.attempt += 1;
    }

    if (!wasOpen && this.failures >= this.failuresBeforePolling) {
      this.startPolling(reason);
      this.scheduleSocket(this.socketRetryWhilePollingMs);
      return;
    }

    const delay = backoffDelay(this.attempt, this.backoff, this.random);
    this.scheduleSocket(delay);
    if (!this.polling) {
      this.setState({ mode: 'reconnecting', attempt: this.attempt, nextRetryAtMs: this.now() + delay, lastError: reason });
    }
  }

  private dropSocket(): void {
    const socket = this.socket;
    this.socket = null;
    this.socketOpen = false;
    if (!socket) return;
    socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
    try {
      socket.close();
    } catch {
      // already closed
    }
  }

  private scheduleSocket(delayMs: number): void {
    this.clearRetry();
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (this.running && !this.socket) this.openSocket();
    }, delayMs);
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private armStale(): void {
    this.clearStale();
    this.staleTimer = setTimeout(() => {
      this.staleTimer = null;
      if (this.socket && this.socketOpen) this.handleEnd(this.socket, 'no updates for a while');
    }, this.staleAfterMs);
  }

  private clearStale(): void {
    if (this.staleTimer !== null) clearTimeout(this.staleTimer);
    this.staleTimer = null;
  }

  private startPolling(reason: string): void {
    if (this.polling) return;
    this.polling = true;
    this.pollFailures = 0;
    this.setState({ mode: 'polling', transport: 'polling', attempt: this.attempt, nextRetryAtMs: null, lastError: reason });
    void this.poll();
  }

  private stopPolling(): void {
    this.polling = false;
    this.pollAbort?.abort();
    this.pollAbort = null;
    if (this.pollTimer !== null) clearTimeout(this.pollTimer);
    this.pollTimer = null;
  }

  private schedulePoll(delayMs: number): void {
    if (this.pollTimer !== null) clearTimeout(this.pollTimer);
    this.pollTimer = setTimeout(() => {
      this.pollTimer = null;
      void this.poll();
    }, delayMs);
  }

  private async poll(): Promise<void> {
    if (!this.polling || !this.running) return;
    const controller = new AbortController();
    this.pollAbort = controller;
    try {
      const snapshot = await this.fetchState(controller.signal);
      if (!this.polling || controller.signal.aborted) return;
      this.pollFailures = 0;
      this.onMessage(asSnapshot(snapshot));
      if (this.state.mode !== 'polling' || this.state.lastError) {
        this.setState({ mode: 'polling', transport: 'polling', nextRetryAtMs: null, lastError: null });
      }
      this.schedulePoll(this.pollIntervalMs);
    } catch (err) {
      if (!this.polling || controller.signal.aborted) return;
      this.pollFailures += 1;
      const delay = Math.max(this.pollIntervalMs, backoffDelay(this.pollFailures, this.backoff, this.random));
      this.setState({
        mode: 'offline',
        transport: 'polling',
        attempt: this.pollFailures,
        nextRetryAtMs: this.now() + delay,
        lastError: err instanceof Error ? err.message : 'request failed',
      });
      this.schedulePoll(delay);
    }
  }
}
