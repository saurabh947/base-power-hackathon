// A three-route router on the History API (Vercel rewrites every path to
// index.html, see web/vercel.json). No dependency needed for /, /live, /results.

import { createStore, useStore } from './store.ts';

export type Route = 'start' | 'live' | 'results' | 'notfound';

export const PATHS: Record<Exclude<Route, 'notfound'>, string> = {
  start: '/',
  live: '/live',
  results: '/results',
};

export function routeOf(pathname: string): Route {
  const path = pathname.replace(/\/+$/, '') || '/';
  if (path === '/') return 'start';
  if (path === '/live') return 'live';
  if (path === '/results') return 'results';
  return 'notfound';
}

const pathStore = createStore(typeof window === 'undefined' ? '/' : window.location.pathname);

if (typeof window !== 'undefined') {
  window.addEventListener('popstate', () => pathStore.set(window.location.pathname));
}

export function navigate(path: string): void {
  if (path !== window.location.pathname) {
    window.history.pushState(null, '', path);
    pathStore.set(path);
  }
  window.scrollTo({ top: 0 });
}

export function useRoute(): Route {
  return useStore(pathStore, (p) => routeOf(p));
}
