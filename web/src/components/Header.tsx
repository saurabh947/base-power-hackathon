import type { Route } from '../lib/router.ts';
import { useBackend } from '../state/backend.ts';
import { setTechnical, useTechnical } from '../state/prefs.ts';
import { Icon } from './Icon.tsx';
import { Link } from './Link.tsx';
import { Switch } from './Switch.tsx';

export function Header({ route }: { route: Route }) {
  const technical = useTechnical();
  const { health } = useBackend();
  const running = health?.status === 'running' || health?.status === 'starting' || health?.status === 'paused';

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
