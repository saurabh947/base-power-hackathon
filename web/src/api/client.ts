// A small fetch wrapper for the REST endpoints in docs/API.md. Every failure
// becomes an ApiError so screens can turn it into a friendly sentence
// (src/api/errors.ts). Nothing here stores the presenter passcode: callers pass it.

import { API_BASE } from './config.ts';
import type {
  ApiErrorBody,
  AvailabilityRequest,
  AvailabilityResponse,
  BacktestStatus,
  BatteryInfo,
  EditableSettings,
  Forecast,
  HealthResponse,
  KillResponse,
  Results,
  RunStatus,
  SettingsResponse,
  StartResponse,
} from './types.ts';

export const PASSCODE_HEADER = 'X-Presenter-Passcode';

export class ApiError extends Error {
  readonly status: number; // HTTP status, or 0 when the server could not be reached
  readonly code: string; // server `error` code, or network | timeout | not_configured | http_<status>
  readonly serverMessage: string | null;
  readonly retryAfterS: number | null;

  constructor(status: number, code: string, serverMessage: string | null, retryAfterS: number | null) {
    super(serverMessage ?? `${code} (${status})`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.serverMessage = serverMessage;
    this.retryAfterS = retryAfterS;
  }
}

export interface RequestOptions {
  body?: unknown;
  passcode?: string | null;
  timeoutMs?: number;
  signal?: AbortSignal;
  /** Fetch cache mode: 'no-store' (default) for live data; 'no-cache' revalidates with an ETag. */
  cache?: RequestCache;
}

function parseRetryAfter(body: ApiErrorBody | null, header: string | null): number | null {
  if (body && typeof body.retry_after_s === 'number' && Number.isFinite(body.retry_after_s)) {
    return Math.max(0, body.retry_after_s);
  }
  if (header) {
    const seconds = Number(header);
    if (Number.isFinite(seconds)) return Math.max(0, seconds);
  }
  return null;
}

export async function request<T>(method: 'GET' | 'POST', path: string, options: RequestOptions = {}): Promise<T> {
  if (!API_BASE) throw new ApiError(0, 'not_configured', null, null);
  const controller = new AbortController();
  const headers: Record<string, string> = {};
  if (options.passcode) headers[PASSCODE_HEADER] = options.passcode;
  const init: RequestInit = { method, headers, signal: controller.signal, cache: options.cache ?? 'no-store' };
  if (options.body !== undefined && method === 'POST') {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }

  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, options.timeoutMs ?? 15_000);
  const onOuterAbort = () => controller.abort();
  options.signal?.addEventListener('abort', onOuterAbort, { once: true });

  let response: Response;
  try {
    response = await fetch(API_BASE + path, init);
  } catch (err) {
    if (options.signal?.aborted) throw err;
    throw new ApiError(0, timedOut ? 'timeout' : 'network', null, null);
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener('abort', onOuterAbort);
  }

  const text = await response.text().catch(() => '');
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!response.ok) {
    const errBody = data && typeof data === 'object' ? (data as ApiErrorBody) : null;
    throw new ApiError(
      response.status,
      errBody?.error ?? `http_${response.status}`,
      typeof errBody?.message === 'string' ? errBody.message : null,
      parseRetryAfter(errBody, response.headers.get('Retry-After')),
    );
  }
  return data as T;
}

export const api = {
  health: (signal?: AbortSignal) => request<HealthResponse>('GET', '/health', { signal, timeoutMs: 30_000 }),
  settings: (signal?: AbortSignal) => request<SettingsResponse>('GET', '/settings', { signal, timeoutMs: 30_000 }),
  state: (signal?: AbortSignal) => request<unknown>('GET', '/state', { signal }),
  // After a Reset the server holds a Start for up to 15 s while its controllers come back up.
  start: (settings: Partial<EditableSettings>) => request<StartResponse>('POST', '/run/start', { body: settings, timeoutMs: 35_000 }),
  pause: () => request<{ status: RunStatus }>('POST', '/run/pause'),
  resume: () => request<{ status: RunStatus }>('POST', '/run/resume'),
  speed: (speed_tps: number) => request<{ speed_tps: number }>('POST', '/run/speed', { body: { speed_tps } }),
  reset: () => request<{ status: RunStatus }>('POST', '/run/reset'),
  availability: (change: AvailabilityRequest) => request<AvailabilityResponse>('POST', '/availability', { body: change }),
  battery: (batteryId: string) => request<BatteryInfo>('GET', `/batteries/${encodeURIComponent(batteryId)}`),
  /** GET /fleet/layout, read defensively by src/map/layout.ts. */
  layout: (signal?: AbortSignal) => request<unknown>('GET', '/fleet/layout', { signal, timeoutMs: 30_000, cache: 'no-cache' }),
  presenterCheck: (passcode: string) => request<{ ok: boolean }>('POST', '/presenter/check', { passcode }),
  kill: (passcode: string, workerId?: number) =>
    request<KillResponse>('POST', workerId === undefined ? '/chaos/kill' : `/chaos/kill?worker_id=${workerId}`, { passcode }),
  backtest: (strategies: string[], year: number) =>
    request<{ job_id: string }>('POST', '/backtest', { body: { strategies, year } }),
  backtestStatus: (jobId: string) => request<BacktestStatus>('GET', `/backtest/${encodeURIComponent(jobId)}`),
  results: (signal?: AbortSignal) => request<Results>('GET', '/results/pitch', { signal, timeoutMs: 30_000 }),
  forecast: (location: string, atUtc: string) =>
    request<Forecast>('GET', `/forecast?location=${encodeURIComponent(location)}&at_utc=${encodeURIComponent(atUtc)}`),
};
