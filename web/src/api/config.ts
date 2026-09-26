// Where the backend lives. VITE_API_BASE_URL is baked in at build time (Vercel
// env var, see web/README.md). In `npm run dev` without it, the dev server's
// simulated mock API at /__mock is used instead; production builds have no
// fallback and show a "not connected" notice.

export const MOCK_PATH = '/__mock';

/** Normalise a configured base URL: absolute, no trailing slash. */
export function resolveApiBase(configured: string | undefined | null, origin: string, devFallback: string | null): string | null {
  const raw = configured?.trim();
  if (!raw) return devFallback;
  try {
    return new URL(raw, origin).toString().replace(/\/+$/, '');
  } catch {
    return null;
  }
}

/** http(s)://host/base -> ws(s)://host/base/live */
export function toWebSocketUrl(apiBase: string, path = '/live'): string {
  const url = new URL(apiBase.replace(/\/+$/, '') + path);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

const origin = typeof window === 'undefined' ? 'http://localhost' : window.location.origin;

export const API_BASE: string | null = resolveApiBase(
  import.meta.env.VITE_API_BASE_URL,
  origin,
  import.meta.env.DEV ? origin + MOCK_PATH : null,
);
