import { useCallback, useEffect, useState } from 'react';
import { Link } from '../../components/Link.tsx';
import { ErrorNotice, Notice } from '../../components/Notice.tsx';
import { holdLiveFeed, reconnectNow, useConnection, useLive } from '../../live/liveStore.ts';
import { useBackend } from '../../state/backend.ts';
import { AvailabilityDrawer } from './AvailabilityDrawer.tsx';
import { ControllersCard } from './ControllersCard.tsx';
import { EventsFeed } from './EventsFeed.tsx';
import { MoneyCard } from './MoneyCard.tsx';
import { PowerCard } from './PowerCard.tsx';
import { PriceCard } from './PriceCard.tsx';
import { TopBar } from './TopBar.tsx';

/** One notice at most, for the state of the run or of the connection. */
function StateNotice() {
  const received = useLive((s) => s.received);
  const status = useLive((s) => s.status);
  const mode = useConnection((s) => s.mode);
  const backend = useBackend();

  if (mode === 'offline') {
    return (
      <ErrorNotice
        error={{
          title: "Can't reach the simulator right now.",
          detail: 'The page keeps trying on its own. What you see is the last data that arrived.',
          retryAfterS: null,
          technical: 'GET /state polling is failing',
          unreachable: true,
        }}
        actions={
          <button type="button" className="btn btn-sm" onClick={reconnectNow}>
            Try again now
          </button>
        }
      />
    );
  }
  if (!received) {
    return (
      <Notice kind="waiting" title={backend.phase === 'waking' ? 'Waking up the simulator… (usually a few seconds)' : 'Connecting to the simulator…'} />
    );
  }
  if (status === 'idle') {
    return (
      <Notice
        kind="info"
        title="No simulation is running right now."
        actions={
          <Link to="/" className="btn btn-primary btn-sm">
            Start one
          </Link>
        }
      >
        Start a replay and everyone on this page will watch it together.
      </Notice>
    );
  }
  if (status === 'starting') {
    return <Notice kind="waiting" title="Starting the simulation: launching 20 controllers…" />;
  }
  if (status === 'finished') {
    return (
      <Notice
        kind="info"
        title="This replay has finished."
        actions={
          <>
            <Link to="/results" className="btn btn-sm">
              See full-year results
            </Link>
            <Link to="/" className="btn btn-sm btn-quiet">
              Start a new replay
            </Link>
          </>
        }
      />
    );
  }
  return null;
}

export function LiveScreen() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  useEffect(() => holdLiveFeed(), []);
  const openDrawer = useCallback(() => setDrawerOpen(true), []);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);

  return (
    <>
      <TopBar onOpenAvailability={openDrawer} />
      <div className="page">
        <h1 className="sr-only">Live simulation</h1>
        <div className="live-state">
          <StateNotice />
        </div>
        <div className="live-grid">
          <div className="span-7 stack">
            <PriceCard />
            <ControllersCard />
          </div>
          <div className="span-5 stack">
            <MoneyCard />
            <PowerCard />
            <EventsFeed />
          </div>
        </div>
      </div>
      <AvailabilityDrawer open={drawerOpen} onClose={closeDrawer} />
    </>
  );
}
