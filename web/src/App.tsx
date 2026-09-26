import { useEffect, useLayoutEffect, useRef } from 'react';
import { Header } from './components/Header.tsx';
import { Link } from './components/Link.tsx';
import { PracticeBanner } from './components/PracticeBanner.tsx';
import { Toasts } from './components/Toasts.tsx';
import { useRoute, type Route } from './lib/router.ts';
import { LiveScreen } from './screens/live/LiveScreen.tsx';
import { ResultsScreen } from './screens/results/ResultsScreen.tsx';
import { StartScreen } from './screens/start/StartScreen.tsx';
import { checkBackend } from './state/backend.ts';

const TITLES: Record<Route, string> = {
  start: 'Fleet Sim: home batteries on the Texas grid',
  live: 'Live · Fleet Sim',
  results: 'Full-year results · Fleet Sim',
  notfound: 'Page not found · Fleet Sim',
};

export function App() {
  const route = useRoute();
  const mainRef = useRef<HTMLElement>(null);
  const firstRender = useRef(true);
  // The Live page is the map, filling the window (docs/UX.md, Live map). Every page shares its dark theme (tokens.css).
  const mapMode = route === 'live';

  useEffect(() => {
    void checkBackend();
  }, []);

  // A layout effect, so it runs before the pages' own effects: a page can name itself more exactly
  // ("Full-year results 2026 · Fleet Sim").
  useLayoutEffect(() => {
    document.title = TITLES[route];
    // After in-app navigation, move focus to the new page so screen readers start there.
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    mainRef.current?.focus({ preventScroll: true });
  }, [route]);

  useEffect(() => {
    const root = document.documentElement;
    if (!mapMode) return;
    root.classList.add('map-mode');
    return () => root.classList.remove('map-mode');
  }, [mapMode]);

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <Header route={route} />
      <PracticeBanner />
      <main id="main" tabIndex={-1} ref={mainRef} className={mapMode ? 'main-map' : `main-${route}`}>
        {route === 'start' ? <StartScreen /> : null}
        {route === 'live' ? <LiveScreen /> : null}
        {route === 'results' ? <ResultsScreen /> : null}
        {route === 'notfound' ? <NotFound /> : null}
      </main>
      <Toasts />
    </>
  );
}

function NotFound() {
  return (
    <div className="page start">
      <h1>That page doesn't exist.</h1>
      <p className="lede">
        Try the <Link to="/">Start page</Link>, the <Link to="/live">live simulation</Link> or the{' '}
        <Link to="/results">full-year results</Link>.
      </p>
    </div>
  );
}
