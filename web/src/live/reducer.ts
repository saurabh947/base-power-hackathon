// Applies WebSocket /live messages (docs/API.md) to the in-memory view of the
// shared run. Pure and defensive: it accepts any parsed JSON, ignores what it
// does not understand, caps what it keeps, and returns the same object for any
// slice that did not change, so subscribers to that slice do not re-render.

import type {
  Availability,
  FailedCommands,
  Forecast,
  LiveEvent,
  Race,
  RunStatus,
  Settings,
  Tick,
  Totals,
  Worker,
} from '../api/types.ts';

/** snapshot.history holds at most 2,000 ticks; keep the same cap for appended frames. */
export const MAX_HISTORY = 2000;
export const MAX_EVENTS = 50;

export interface LiveState {
  /** At least one snapshot, frame or idle message has been applied. */
  received: boolean;
  runId: string | null;
  status: RunStatus | null;
  settings: Settings | null;
  simTimeUtc: string | null;
  speedTps: number | null;
  /** Oldest first, at most MAX_HISTORY ticks. */
  history: Tick[];
  totals: Totals;
  workers: Worker[];
  failedCommands: FailedCommands;
  availability: Availability | null;
  /** Newest first, at most MAX_EVENTS. */
  events: LiveEvent[];
  /** Latest forecast per location. */
  forecast: Record<string, Forecast>;
  /** Messages applied so far (shown with technical details). */
  messages: number;
  lastMessageAtMs: number | null;
  /** Bumped when a frame belongs to a run we have no snapshot for; the store then re-fetches GET /state. */
  runChanged: number;
  /** From idle messages: the run that was replaced or reset, with its totals. */
  lastRun: LastRun | null;
  /**
   * battery_status of the latest snapshot or frame: base64 of packed 4-bit codes, one per
   * battery (src/map/status.ts). Null when none arrived, e.g. after a reset (every battery online).
   */
  batteryStatus: string | null;
  /**
   * The race: the same fleet on the comparison strategy (docs/API.md Race), from the latest snapshot or
   * frame. Null while idle, and when the backend sends none.
   */
  race: Race | null;
}

export interface LastRun {
  runId: string | null;
  totals: Totals;
}

export const EMPTY_TOTALS: Totals = Object.freeze({ revenue_usd: 0, sold_kwh: 0, bought_kwh: 0, ticks: 0 });
export const EMPTY_FAILED: FailedCommands = Object.freeze({ count: 0, recent: [] });
const EMPTY_HISTORY: Tick[] = [];
const EMPTY_FORECAST: Record<string, Forecast> = Object.freeze({}) as Record<string, Forecast>;

export const initialLiveState: LiveState = Object.freeze({
  received: false,
  runId: null,
  status: null,
  settings: null,
  simTimeUtc: null,
  speedTps: null,
  history: EMPTY_HISTORY,
  totals: EMPTY_TOTALS,
  workers: [],
  failedCommands: EMPTY_FAILED,
  availability: null,
  events: [],
  forecast: EMPTY_FORECAST,
  messages: 0,
  lastMessageAtMs: null,
  runChanged: 0,
  lastRun: null,
  batteryStatus: null,
  race: null,
});

const STATUSES: ReadonlySet<string> = new Set(['idle', 'starting', 'running', 'paused', 'finished']);

type Json = Record<string, unknown>;

function isObject(v: unknown): v is Json {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isTick(v: unknown): v is Tick {
  return isObject(v) && typeof v.ts_utc === 'string' && Number.isFinite(Date.parse(v.ts_utc));
}

function isEvent(v: unknown): v is LiveEvent {
  return isObject(v) && typeof v.id === 'string' && typeof v.message === 'string';
}

function isForecast(v: unknown): v is Forecast {
  return isObject(v) && Array.isArray(v.points);
}

function status(v: unknown): RunStatus | null {
  return typeof v === 'string' && STATUSES.has(v) ? (v as RunStatus) : null;
}

function finiteNumber(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** Keep the previous object when the new one is structurally identical. */
function keep<T>(prev: T, next: unknown, valid: (v: unknown) => boolean = isObject): T {
  if (!valid(next)) return prev;
  if (prev !== null && prev !== undefined && JSON.stringify(prev) === JSON.stringify(next)) return prev;
  return next as T;
}

function tickTime(t: Tick): number {
  return Date.parse(t.ts_utc);
}

function sortedTicks(list: unknown): Tick[] {
  if (!Array.isArray(list)) return [];
  const ticks = list.filter(isTick);
  for (let i = 1; i < ticks.length; i++) {
    if (tickTime(ticks[i]) <= tickTime(ticks[i - 1])) {
      const byTime = new Map<number, Tick>();
      for (const t of ticks) byTime.set(tickTime(t), t);
      return [...byTime.entries()].toSorted((a, b) => a[0] - b[0]).map(([, t]) => t);
    }
  }
  return ticks;
}

function cap(history: Tick[]): Tick[] {
  return history.length > MAX_HISTORY ? history.slice(history.length - MAX_HISTORY) : history;
}

/** Append ticks newer than the last one we hold; ticks are never sampled, so none are dropped. */
export function appendTicks(history: Tick[], incoming: unknown): Tick[] {
  const ticks = sortedTicks(incoming);
  if (!ticks.length) return history;
  const last = history.length ? tickTime(history[history.length - 1]) : -Infinity;
  const fresh = ticks.filter((t) => tickTime(t) > last);
  if (!fresh.length) return history;
  return cap(history.concat(fresh));
}

function sameHistory(a: Tick[], b: Tick[]): boolean {
  return (
    a.length === b.length &&
    (a.length === 0 || (a[0].ts_utc === b[0].ts_utc && a[a.length - 1].ts_utc === b[b.length - 1].ts_utc))
  );
}

function eventTime(e: LiveEvent): number {
  const t = Date.parse(e.wall_utc);
  return Number.isFinite(t) ? t : 0;
}

/** Newest first, de-duplicated by id, capped. `incoming` is in the order the server sent it. */
export function mergeEvents(existing: LiveEvent[], incoming: unknown): LiveEvent[] {
  if (!Array.isArray(incoming)) return existing;
  const seen = new Set(existing.map((e) => e.id));
  const fresh: LiveEvent[] = [];
  for (const e of incoming) {
    if (isEvent(e) && !seen.has(e.id)) {
      seen.add(e.id);
      fresh.push(e);
    }
  }
  if (!fresh.length) return existing;
  // Latest arrival first among equal timestamps; Array.prototype.sort is stable.
  const newestFirst = fresh.toReversed().toSorted((a, b) => eventTime(b) - eventTime(a));
  return newestFirst.concat(existing).slice(0, MAX_EVENTS);
}

function forecasts(v: unknown): Record<string, Forecast> {
  const out: Record<string, Forecast> = {};
  if (!isObject(v)) return out;
  for (const [location, f] of Object.entries(v)) if (isForecast(f)) out[location] = f;
  return out;
}

function lastTs(history: Tick[]): string | null {
  return history.length ? history[history.length - 1].ts_utc : null;
}

const EMPTY_WORKERS: Worker[] = [];

/** Every battery back online, keeping the known totals (what a reset does). */
function allOnline(a: Availability | null): Availability | null {
  if (!a) return null;
  if (a.available === a.total && a.offline_workers.length === 0) return a;
  const byLocation: Availability['by_location'] = {};
  for (const [loc, v] of Object.entries(a.by_location ?? {})) byLocation[loc] = { available: v.total, total: v.total };
  return { available: a.total, total: a.total, by_location: byLocation, offline_workers: [] };
}

function readLastRun(prev: LastRun | null, v: unknown): LastRun | null {
  if (v === undefined) return prev;
  if (!isObject(v) || !isObject(v.totals)) return null;
  const next: LastRun = { runId: typeof v.run_id === 'string' ? v.run_id : null, totals: v.totals as unknown as Totals };
  return prev && prev.runId === next.runId && JSON.stringify(prev.totals) === JSON.stringify(next.totals) ? prev : next;
}

function isRace(v: unknown): v is Race {
  return isObject(v) && typeof v.revenue_usd === 'number' && Number.isFinite(v.revenue_usd);
}

/** The message's race, or null (idle, none sent, or unreadable); the previous object when nothing changed. */
function readRace(prev: Race | null, v: unknown): Race | null {
  return isRace(v) ? keep(prev, v, isRace) : null;
}

function statusOf(message: Json): string | null {
  return typeof message.battery_status === 'string' && message.battery_status ? message.battery_status : null;
}

function runIdOf(message: Json, fallback: string | null): string | null {
  if (!('run_id' in message)) return fallback;
  return typeof message.run_id === 'string' ? message.run_id : null;
}

export function liveReducer(state: LiveState, message: unknown, nowMs: number = Date.now()): LiveState {
  if (!isObject(message)) return state;
  const common = { received: true, messages: state.messages + 1, lastMessageAtMs: nowMs };
  const type = message.type === 'frame' && message.status === 'idle' ? 'idle' : message.type;

  switch (type) {
    case 'snapshot': {
      const runId = runIdOf(message, null);
      const sameRun = runId === state.runId;
      const incoming = cap(sortedTicks(message.history));
      let history = incoming;
      if (sameRun && sameHistory(state.history, incoming)) {
        history = state.history;
      } else if (sameRun && incoming.length && state.history.length) {
        // A re-fetched snapshot can be a moment older than frames we already applied: keep those.
        const lastIncoming = tickTime(incoming[incoming.length - 1]);
        const newer = state.history.filter((t) => tickTime(t) > lastIncoming);
        if (newer.length) history = cap(incoming.concat(newer));
      }
      const settings = isObject(message.settings) ? keep<Settings | null>(state.settings, message.settings) : null;
      const fc = forecasts(message.forecast);
      return {
        ...state,
        ...common,
        runId,
        status: status(message.status) ?? (runId ? state.status : 'idle'),
        settings,
        simTimeUtc: typeof message.sim_time_utc === 'string' ? message.sim_time_utc : lastTs(history),
        speedTps: finiteNumber(message.speed_tps) ?? finiteNumber(settings?.speed_tps) ?? state.speedTps,
        history,
        totals: keep(sameRun ? state.totals : EMPTY_TOTALS, message.totals),
        workers: keep(state.workers, message.workers, Array.isArray),
        failedCommands: keep(sameRun ? state.failedCommands : EMPTY_FAILED, message.failed_commands),
        availability: keep(state.availability, message.availability),
        events: mergeEvents(sameRun ? state.events : [], message.events),
        forecast: JSON.stringify(fc) === JSON.stringify(state.forecast) ? state.forecast : fc,
        batteryStatus: statusOf(message),
        race: readRace(sameRun ? state.race : null, message.race),
      };
    }

    case 'frame': {
      const runId = runIdOf(message, state.runId);
      const newRun = runId !== state.runId;
      const history = appendTicks(newRun ? EMPTY_HISTORY : state.history, message.ticks);
      let forecast = newRun ? EMPTY_FORECAST : state.forecast;
      if (message.forecast !== undefined) {
        const fc = forecasts(message.forecast);
        if (Object.keys(fc).length) forecast = { ...forecast, ...fc };
      }
      return {
        ...state,
        ...common,
        runId,
        status: status(message.status) ?? state.status,
        settings: newRun ? null : state.settings,
        simTimeUtc: typeof message.sim_time_utc === 'string' ? message.sim_time_utc : (lastTs(history) ?? state.simTimeUtc),
        speedTps: finiteNumber(message.speed_tps) ?? state.speedTps,
        history,
        totals: keep(newRun ? EMPTY_TOTALS : state.totals, message.totals),
        workers: keep(state.workers, message.workers, Array.isArray),
        failedCommands: keep(newRun ? EMPTY_FAILED : state.failedCommands, message.failed_commands),
        availability: keep(state.availability, message.availability),
        events: mergeEvents(newRun ? [] : state.events, message.events),
        forecast,
        runChanged: newRun ? state.runChanged + 1 : state.runChanged,
        batteryStatus: statusOf(message),
        race: readRace(newRun ? null : state.race, message.race),
      };
    }

    case 'idle': {
      // {"type": "idle", "status": "idle", "run_id": null, "last_run": {run_id, totals} | null}, every 5 s.
      const hadRun = state.runId !== null || state.history.length > 0;
      return {
        ...state,
        ...common,
        runId: null,
        status: 'idle',
        settings: hadRun ? null : state.settings,
        simTimeUtc: hadRun ? null : state.simTimeUtc,
        history: EMPTY_HISTORY,
        totals: EMPTY_TOTALS,
        failedCommands: hadRun ? EMPTY_FAILED : state.failedCommands,
        // Reset restarts every controller and brings every battery back, so the last run's view is stale.
        workers: hadRun ? EMPTY_WORKERS : keep(state.workers, message.workers, Array.isArray),
        availability: hadRun ? allOnline(state.availability) : keep(state.availability, message.availability),
        events: mergeEvents(state.events, message.events),
        forecast: EMPTY_FORECAST,
        lastRun: readLastRun(state.lastRun, message.last_run),
        // A reset brings every battery back online; idle messages carry no battery_status.
        batteryStatus: hadRun ? null : (statusOf(message) ?? state.batteryStatus),
        // No run, no race.
        race: null,
      };
    }

    default:
      return state;
  }
}
