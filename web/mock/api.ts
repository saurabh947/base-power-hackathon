// The mock API's HTTP routes and WebSocket /live, per docs/API.md, on top of
// the SIMULATED fleet in ./sim.ts. Framework-free so it plugs into both the
// standalone server (./server.ts) and the Vite dev server (./vitePlugin.ts).
// Dev and tests only; never part of a production build.

import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Duplex } from 'node:stream';
import { WebSocketServer, type WebSocket } from 'ws';
import type { AvailabilityRequest, EditableSettings } from '../src/api/types.ts';
import { MOCK_MARKER, MOCK_VERSION, MockError, MockSim, mockSettingsResponse } from './sim.ts';

export { MOCK_MARKER };

export interface MockApiOptions {
  /** Path prefix, e.g. "/__mock" inside the Vite dev server; "" for the standalone server. */
  basePath?: string;
  /** Presenter passcode; null or "" means "not set" (POST /chaos/kill answers 503). */
  passcode?: string | null;
  /** Simulate a Cloud Run cold start: hold requests (and refuse WebSockets) for this long after boot. */
  coldStartMs?: number;
  /** false refuses WebSocket upgrades so the site falls back to polling GET /state. */
  websocket?: boolean;
  /** 404 from GET /results/pitch until a full-year run finishes. */
  noResultsUntilBacktest?: boolean;
  backtestFails?: boolean;
  frameMs?: number;
}

export function mockOptionsFromEnv(env: NodeJS.ProcessEnv): MockApiOptions {
  return {
    passcode: env.MOCK_PRESENTER_PASSCODE ?? 'presenter',
    coldStartMs: Number(env.MOCK_COLD_START_MS ?? 0) || 0,
    websocket: env.MOCK_WS !== 'off',
    noResultsUntilBacktest: env.MOCK_NO_RESULTS === '1',
    backtestFails: env.MOCK_BACKTEST_FAIL === '1',
  };
}

class RateLimiter {
  private readonly hits: number[] = [];
  private readonly limit: number;
  private readonly windowMs: number;
  constructor(limit: number, windowMs: number) {
    this.limit = limit;
    this.windowMs = windowMs;
  }
  /** null when allowed, else seconds until the next call is allowed. */
  check(now: number): number | null {
    while (this.hits.length && now - this.hits[0] >= this.windowMs) this.hits.shift();
    if (this.hits.length >= this.limit) return Math.max(1, Math.ceil((this.windowMs - (now - this.hits[0])) / 1000));
    this.hits.push(now);
    return null;
  }
}

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, X-Presenter-Passcode',
  'Access-Control-Expose-Headers': 'Retry-After',
  'Access-Control-Max-Age': '600',
};

function send(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...CORS, ...headers });
  res.end(JSON.stringify(body));
}

function sendError(res: ServerResponse, err: MockError): void {
  const body: Record<string, unknown> = { error: err.code, message: err.message };
  const headers: Record<string, string> = {};
  if (err.retryAfterS !== null) {
    body.retry_after_s = err.retryAfterS;
    headers['Retry-After'] = String(err.retryAfterS);
  }
  send(res, err.status, body, headers);
}

async function readJson(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > 64 * 1024) throw new MockError(413, 'too_large', 'That request is too large.');
    chunks.push(chunk as Buffer);
  }
  if (!size) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    throw new MockError(400, 'bad_json', 'The request body is not valid JSON.');
  }
}

export interface MockApi {
  handle(req: IncomingMessage, res: ServerResponse): Promise<boolean>;
  upgrade(req: IncomingMessage, socket: Duplex, head: Buffer): boolean;
  close(): void;
  sim: MockSim;
}

export function createMockApi(options: MockApiOptions = {}): MockApi {
  const base = (options.basePath ?? '').replace(/\/+$/, '');
  const passcode = options.passcode ?? null;
  const bootAt = Date.now();
  const coldStartMs = options.coldStartMs ?? 0;
  const sim = new MockSim({
    backtestFails: options.backtestFails,
    resultsBeforeBacktest: !options.noResultsUntilBacktest,
  });
  const wss = new WebSocketServer({ noServer: true });
  const clients = new Set<WebSocket>();
  const runLimit = new RateLimiter(1, 5000);
  const availabilityLimit = new RateLimiter(10, 1000);
  const wrongPasscodes = new Map<string, RateLimiter>();

  // A frame every second while a run exists; an idle message every 5 s when none does.
  const timer = setInterval(() => {
    const next = sim.stepWall();
    if (!next) return;
    const message = JSON.stringify(next);
    for (const ws of clients) if (ws.readyState === ws.OPEN) ws.send(message);
  }, options.frameMs ?? 1000);
  timer.unref?.();

  wss.on('connection', (ws: WebSocket) => {
    clients.add(ws);
    ws.send(JSON.stringify(sim.snapshot()));
    ws.on('close', () => clients.delete(ws));
    ws.on('error', () => clients.delete(ws));
  });

  const cold = () => Math.max(0, coldStartMs - (Date.now() - bootAt));

  const limited = (limiter: RateLimiter) => {
    const wait = limiter.check(Date.now());
    if (wait !== null) {
      throw new MockError(429, 'rate_limited', 'Everyone shares one fleet, so this button is rate-limited. Try again shortly.', wait);
    }
  };

  const requirePresenter = (req: IncomingMessage) => {
    if (!passcode) throw new MockError(503, 'presenter_passcode_not_set', 'No presenter passcode is set on this server.');
    const given = req.headers['x-presenter-passcode'];
    if (given !== passcode) {
      const ip = req.socket.remoteAddress ?? 'unknown';
      let limiter = wrongPasscodes.get(ip);
      if (!limiter) {
        limiter = new RateLimiter(5, 60_000);
        wrongPasscodes.set(ip, limiter);
      }
      const wait = limiter.check(Date.now());
      if (wait !== null) throw new MockError(429, 'rate_limited', 'Too many wrong passcodes. Wait a minute.', wait);
      throw new MockError(403, 'forbidden', 'That passcode is not right.');
    }
  };

  async function route(req: IncomingMessage, res: ServerResponse, path: string, url: URL): Promise<void> {
    const method = req.method ?? 'GET';
    const key = `${method} ${path}`;
    switch (key) {
      case 'GET /health':
        return send(res, 200, { ok: true, version: MOCK_VERSION, status: sim.status });
      case 'GET /settings':
        return send(res, 200, mockSettingsResponse());
      case 'GET /state':
        return send(res, 200, sim.snapshot());
      case 'POST /run/start': {
        limited(runLimit);
        const body = (await readJson(req)) as Partial<EditableSettings>;
        return send(res, 202, sim.start(body ?? {}));
      }
      case 'POST /run/pause':
        return send(res, 200, sim.pause());
      case 'POST /run/resume':
        return send(res, 200, sim.resume());
      case 'POST /run/speed': {
        const body = (await readJson(req)) as { speed_tps?: number };
        return send(res, 200, sim.setSpeed(Number(body?.speed_tps)));
      }
      case 'POST /run/reset':
        limited(runLimit);
        return send(res, 200, sim.reset());
      case 'POST /availability': {
        limited(availabilityLimit);
        const body = (await readJson(req)) as AvailabilityRequest;
        return send(res, 200, sim.setAvailability(body));
      }
      case 'POST /presenter/check':
        requirePresenter(req);
        return send(res, 200, { ok: true });
      case 'POST /chaos/kill': {
        requirePresenter(req);
        const raw = url.searchParams.get('worker_id');
        return send(res, 200, sim.kill(raw === null || raw === '' ? undefined : Number(raw)));
      }
      case 'POST /backtest':
        return send(res, 202, sim.startBacktest());
      case 'GET /results/pitch':
        return send(res, 200, sim.results());
      case 'GET /forecast': {
        const f = sim.forecastAt(url.searchParams.get('location') ?? '', url.searchParams.get('at_utc'));
        if (!f) throw new MockError(404, 'no_forecast', 'No forecast has been issued for that area and time yet.');
        return send(res, 200, f);
      }
      default:
        break;
    }
    if (method === 'GET' && path.startsWith('/batteries/')) {
      return send(res, 200, sim.battery(decodeURIComponent(path.slice('/batteries/'.length))));
    }
    if (method === 'GET' && path.startsWith('/backtest/')) {
      return send(res, 200, sim.backtestStatus(decodeURIComponent(path.slice('/backtest/'.length))));
    }
    throw new MockError(404, 'not_found', `The practice server has no ${method} ${path}.`);
  }

  async function handle(req: IncomingMessage, res: ServerResponse): Promise<boolean> {
    const url = new URL(req.url ?? '/', 'http://mock.local');
    if (base && url.pathname !== base && !url.pathname.startsWith(`${base}/`)) return false;
    const path = url.pathname.slice(base.length) || '/';
    if (req.method === 'OPTIONS') {
      res.writeHead(204, CORS);
      res.end();
      return true;
    }
    const wait = cold();
    if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
    try {
      await route(req, res, path, url);
    } catch (err) {
      if (err instanceof MockError) sendError(res, err);
      else send(res, 500, { error: 'internal', message: 'The practice server hit an error.' });
    }
    return true;
  }

  function upgrade(req: IncomingMessage, socket: Duplex, head: Buffer): boolean {
    const url = new URL(req.url ?? '/', 'http://mock.local');
    if (url.pathname !== `${base}/live`) return false;
    if (options.websocket === false || cold() > 0) {
      socket.destroy();
      return true;
    }
    wss.handleUpgrade(req, socket, head, (ws) => wss.emit('connection', ws, req));
    return true;
  }

  function close(): void {
    clearInterval(timer);
    for (const ws of clients) ws.terminate();
    wss.close();
  }

  return { handle, upgrade, close, sim };
}
