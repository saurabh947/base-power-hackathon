import type { Route } from '../lib/router.ts';
import { useConnection, useLive } from '../live/liveStore.ts';
import { useBackend } from '../state/backend.ts';
import { setTechnical, useTechnical } from '../state/prefs.ts';
import { Icon } from './Icon.tsx';
import { Link } from './Link.tsx';
import { Switch } from './Switch.tsx';

const ACTIVE = new Set(['running', 'starting', 'paused']);

export function Header({ route }: { route: Route }) {
  const technical = useTechnical();
  const { health } = useBackend();
  // While the live feed is connected it knows best; otherwise use the last GET /health.
  const feedUp = useConnection((c) => c.mode === 'live' || c.mode === 'polling');
  const liveStatus = useLive((s) => (s.received ? s.status : null));
  const status = feedUp && liveStatus ? liveStatus : health?.status;
  const running = !!status && ACTIVE.has(status);

  const current = (r: Route) => (route === r ? ('page' as const) : undefined);

  return (
    <header className="site-header">
      <div className="site-header-inner">
        <Link to="/" className="brand">
          <Icon name="battery" size={24} />
          Fleet Sim
        </Link>
        <nav className="main-nav" aria-label="Main">
          <ul>
            <li>
              <Link to="/" aria-current={current('start')}>
                Start
              </Link>
            </li>
            <li>
              <Link to="/live" aria-current={current('live')}>
                Live
                {running ? (
                  <>
                    <span className="dot dot-ok dot-pulse" aria-hidden="true" />
                    <span className="sr-only">(a simulation is running)</span>
                  </>
                ) : null}
              </Link>
            </li>
            <li>
              <Link to="/results" aria-current={current('results')}>
                Results
              </Link>
            </li>
          </ul>
        </nav>
        <div className="header-spacer" />
        <Switch checked={technical} onChange={setTechnical} label="Show technical details" />
      </div>
    </header>
  );
}
