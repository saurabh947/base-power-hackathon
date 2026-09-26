import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, request } from './client.ts';
import { friendlyError } from './errors.ts';

describe('friendlyError', () => {
  it('turns 429 into a wait with the server countdown', () => {
    const f = friendlyError(new ApiError(429, 'rate_limited', null, 4), 'start');
    expect(f.title).toBe('Too many requests right now.');
    expect(f.detail).toContain('Try again in 4 seconds.');
    expect(f.retryAfterS).toBe(4);
    expect(f.unreachable).toBe(false);
  });

  it('explains the one-crash-every-2-seconds limit in plain words', () => {
    const f = friendlyError(new ApiError(429, 'rate_limited', null, 2), 'kill');
    expect(f.title).toBe('One crash every 2 seconds, please.');
    expect(f.detail).toBe('Everyone shares one fleet, so crashes are spaced out. Try again in 2 seconds.');
    expect(f.retryAfterS).toBe(2); // the crash buttons count down
    expect(friendlyError(new ApiError(429, 'rate_limited', null, null), 'kill').detail).toContain('Try again in a few seconds.');
  });

  it('explains 501 as a feature that is not switched on, using the server sentence', () => {
    const f = friendlyError(new ApiError(501, 'not_implemented', 'Forecasts land in M5.', null));
    expect(f.title).toBe("This part isn't switched on yet.");
    expect(f.detail).toBe('Forecasts land in M5.');
  });

  it('treats 503 as busy or starting', () => {
    const busy = friendlyError(new ApiError(503, 'http_503', null, null));
    expect(busy.title).toBe('The simulator is busy or still starting.');
    expect(busy.unreachable).toBe(true);
  });

  it('asks for a short wait when a Start comes while the controllers restart after a Reset', () => {
    const f = friendlyError(new ApiError(503, 'simulator_restarting', 'The simulator is still restarting its controllers.', 5), 'start');
    expect(f.title).toBe('The simulator is still restarting after a reset.');
    expect(f.detail).toBe('Its 20 controllers are coming back up. Try again in 5 seconds.');
    expect(f.retryAfterS).toBe(5); // the Start button counts down
    expect(f.unreachable).toBe(false);
  });

  it('covers network failures, timeouts and a missing base URL', () => {
    expect(friendlyError(new ApiError(0, 'network', null, null)).title).toBe("Can't reach the simulator.");
    expect(friendlyError(new ApiError(0, 'timeout', null, null)).title).toBe('The simulator is taking a long time to answer.');
    expect(friendlyError(new ApiError(0, 'not_configured', null, null)).title).toBe(
      'This site is not connected to a simulator.',
    );
  });

  it('names the crash errors plainly and says what to do next', () => {
    const dead = friendlyError(new ApiError(409, 'already_dead', 'That worker is already dead.', null), 'kill');
    expect(dead.title).toBe('That controller has already crashed.');
    expect(dead.detail).toBe('Pick one that is still working. Crashed controllers stay down until someone presses Reset.');
    const few = friendlyError(new ApiError(409, 'too_few_healthy', null, null), 'kill');
    expect(few.title).toBe('At least 2 controllers must keep working.');
    expect(few.detail).toBe('Press Reset to bring the crashed controllers back, then try again.');
    expect(friendlyError(new ApiError(404, 'unknown_worker', null, null), 'kill').detail).toBe('Controllers are numbered 1 to 20.');
  });

  it('does not repeat the server sentence for "no_results", and says what to do', () => {
    const f = friendlyError(new ApiError(404, 'no_results', 'No full-year results yet.', null), 'results');
    expect(f.title).toBe('No full-year results yet.');
    expect(f.detail).toBe('Press "Re-run the full year" to calculate them.');
    expect(f.unreachable).toBe(false);
  });

  it('keeps the raw status for "Show technical details"', () => {
    expect(friendlyError(new ApiError(409, 'already_dead', 'That worker is already dead.', null), 'kill').technical).toBe(
      'HTTP 409 · already_dead · That worker is already dead.',
    );
  });

  it('handles errors that are not ApiErrors', () => {
    expect(friendlyError(new Error('boom')).title).toBe('Something went wrong.');
  });
});

describe('request', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('sends JSON with only a content type, and returns the body', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(request('POST', '/availability', { body: { a: 1 } })).resolves.toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url.endsWith('/availability')).toBe(true);
    // Every endpoint is public: no credential headers (docs/API.md CORS allows Content-Type only).
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
    expect(init.body).toBe('{"a":1}');
  });

  it('crashes a controller with a bare POST, a chosen one by worker_id', async () => {
    const fetchMock = vi.fn<typeof fetch>(
      async () => new Response(JSON.stringify({ worker_id: 6, pid: 1, killed_at_utc: '2026-01-01T00:00:00Z' }), { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    await api.kill(6);
    await api.kill();
    const [first, second] = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(first[0].endsWith('/chaos/kill?worker_id=6')).toBe(true);
    expect(second[0].endsWith('/chaos/kill')).toBe(true);
    expect(first[1]).toMatchObject({ method: 'POST', headers: {} });
    expect(first[1].body).toBeUndefined();
  });

  it('does not send a body or content type on GET', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await request('GET', '/health', { body: { ignored: true } });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.body).toBeUndefined();
    expect((init.headers as Record<string, string>)['Content-Type']).toBeUndefined();
  });

  it('turns an error body into an ApiError, with retry_after_s or Retry-After', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ error: 'rate_limited', retry_after_s: 3 }), { status: 429 })),
    );
    await expect(request('POST', '/run/start')).rejects.toMatchObject({ status: 429, code: 'rate_limited', retryAfterS: 3 });

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('busy', { status: 503, headers: { 'Retry-After': '7' } })),
    );
    await expect(request('GET', '/state')).rejects.toMatchObject({ status: 503, code: 'http_503', retryAfterS: 7 });
  });

  it('reports a network failure as status 0', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    await expect(request('GET', '/health')).rejects.toMatchObject({ status: 0, code: 'network' });
  });
});
