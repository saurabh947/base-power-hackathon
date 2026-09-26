import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, PASSCODE_HEADER, request } from './client.ts';
import { friendlyError } from './errors.ts';

describe('friendlyError', () => {
  it('turns 429 into a wait with the server countdown', () => {
    const f = friendlyError(new ApiError(429, 'rate_limited', null, 4), 'start');
    expect(f.title).toBe('Too many requests right now.');
    expect(f.detail).toContain('Try again in 4 seconds.');
    expect(f.retryAfterS).toBe(4);
    expect(f.unreachable).toBe(false);
  });

  it('has its own words for too many wrong passcodes', () => {
    expect(friendlyError(new ApiError(429, 'rate_limited', null, 60), 'presenter').title).toBe('Too many wrong passcodes.');
  });

  it('explains 501 as a feature that is not switched on, using the server sentence', () => {
    const f = friendlyError(new ApiError(501, 'not_implemented', 'Forecasts land in M5.', null));
    expect(f.title).toBe("This part isn't switched on yet.");
    expect(f.detail).toBe('Forecasts land in M5.');
  });

  it('treats 503 as busy or starting, except a missing presenter passcode', () => {
    const busy = friendlyError(new ApiError(503, 'http_503', null, null));
    expect(busy.title).toBe('The simulator is busy or still starting.');
    expect(busy.unreachable).toBe(true);
    const noPasscode = friendlyError(new ApiError(503, 'presenter_passcode_not_set', null, null), 'kill');
    expect(noPasscode.title).toBe("Presenter mode isn't set up on the server yet.");
    expect(noPasscode.unreachable).toBe(false);
  });

  it('covers network failures, timeouts and a missing base URL', () => {
    expect(friendlyError(new ApiError(0, 'network', null, null)).title).toBe("Can't reach the simulator.");
    expect(friendlyError(new ApiError(0, 'timeout', null, null)).title).toBe('The simulator is taking a long time to answer.');
    expect(friendlyError(new ApiError(0, 'not_configured', null, null)).title).toBe(
      'This site is not connected to a simulator.',
    );
  });

  it('names the chaos errors plainly', () => {
    expect(friendlyError(new ApiError(409, 'already_dead', null, null), 'kill').title).toBe(
      'That controller has already crashed.',
    );
    expect(friendlyError(new ApiError(409, 'too_few_healthy', null, null), 'kill').title).toBe(
      'At least 2 controllers must keep working.',
    );
    expect(friendlyError(new ApiError(403, 'forbidden', null, null), 'kill').title).toBe("That passcode wasn't accepted.");
  });

  it('does not repeat the server sentence for "no_results", and says what to do', () => {
    const f = friendlyError(new ApiError(404, 'no_results', 'No full-year results yet.', null), 'results');
    expect(f.title).toBe('No full-year results yet.');
    expect(f.detail).toBe('Press "Re-run the full year" to calculate them.');
    expect(f.unreachable).toBe(false);
  });

  it('keeps the raw status for "Show technical details"', () => {
    expect(friendlyError(new ApiError(404, 'unknown_battery', 'No battery has that ID.', null), 'battery').technical).toBe(
      'HTTP 404 · unknown_battery · No battery has that ID.',
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

  it('sends JSON and the presenter header, and returns the body', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(request('POST', '/chaos/kill', { body: { a: 1 }, passcode: 'secret-test' })).resolves.toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url.endsWith('/chaos/kill')).toBe(true);
    expect((init.headers as Record<string, string>)[PASSCODE_HEADER]).toBe('secret-test');
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(init.body).toBe('{"a":1}');
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
