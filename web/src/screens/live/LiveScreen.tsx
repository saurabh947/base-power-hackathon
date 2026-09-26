import { useCallback, useEffect, useState } from 'react';
import { Link } from '../../components/Link.tsx';
import { ErrorNotice, Notice } from '../../components/Notice.tsx';
import { holdLiveFeed, reconnectNow, useConnection, useLive } from '../../live/liveStore.ts';
import { fmtClock, fmtDuration, fmtEnergy, fmtUsd } from '../../lib/format.ts';
import { onSelectionChange } from '../../map/selection.ts';
import { useBackend } from '../../state/backend.ts';
import { AvailabilityDrawer } from './AvailabilityDrawer.tsx';
import { ControllersCard } from './ControllersCard.tsx';
import { EventsFeed } from './EventsFeed.tsx';
import { LiveMap } from './map/LiveMap.tsx';
import { MoneyCard } from './MoneyCard.tsx';
import { PowerCard } from './PowerCard.tsx';
import { PriceCard } from './PriceCard.tsx';
import { TopBar } from './TopBar.tsx';
import { setView, useView } from './viewStore.ts';

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
        <LastRunLine />
      </Notice>
    );
  }
  if (status === 'starting') {
    return <Notice kind="waiting" title="Starting the simulation: launching 20 controllers…" />;
  }
  if (status === 'finished') {
    return <FinishedNotice />;
  }
  return null;
}

/** Whether StateNotice has anything to say (so the map can leave its notice area empty). */
function useHasNotice(): boolean {
  const received = useLive((s) => s.received);
  const status = useLive((s) => s.status);
  const mode = useConnection((s) => s.mode);
  return mode === 'offline' || !received || status === 'idle' || status === 'starting' || status === 'finished';
}

/** "Replay finished": the run reached the end of the loaded prices (event run_finished). */
function FinishedNotice() {
  const totals = useLive((s) => s.totals);
  const finishedAt = useLive((s) => s.events.find((e) => e.kind === 'run_finished')?.sim_utc ?? s.simTimeUtc);
  return (
    <Notice
      kind="info"
      title="Replay finished"
      actions={
        <>
          <Link to="/" className="btn btn-primary btn-sm">
            Start a new replay
          </Link>
          <Link to="/results" className="btn btn-sm">
            See full-year results
          </Link>
        </>
      }
    >
      The replay reached the end of its recorded prices{finishedAt ? ` at ${fmtClock(finishedAt)}` : ''}. The fleet
      earned <strong>{fmtUsd(totals.revenue_usd)}</strong> in gross wholesale value, selling{' '}
      {fmtEnergy(totals.sold_kwh)} and buying {fmtEnergy(totals.bought_kwh)}.
    </Notice>
  );
}

/** From the idle message's last_run: what the replaced or reset run earned. */
function LastRunLine() {
  const lastRun = useLive((s) => s.lastRun);
  if (!lastRun) return null;
  return (
    <>
      {' '}
      The last replay earned <strong>{fmtUsd(lastRun.totals.revenue_usd)}</strong> in gross wholesale value over{' '}
      {fmtDuration(lastRun.totals.ticks * 300)} of market time.
    </>
  );
}

/** The detailed layout behind "Show charts": everything from before the map, nothing lost. */
function ChartsView({ onOpenAvailability, onShowMap }: { onOpenAvailability: () => void; onShowMap: () => void }) {
  return (
    <>
      <TopBar onOpenAvailability={onOpenAvailability} onShowMap={onShowMap} />
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
    </>
  );
}

export function LiveScreen() {
  const mode = useView((v) => v.mode);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const hasNotice = useHasNotice();
  useEffect(() => holdLiveFeed(), []);
  // A city picked on the map becomes the price chart's area in the detailed layout.
  useEffect(() => {
    onSelectionChange((location) => {
      if (location) setView({ area: location });
    });
    return () => onSelectionChange(null);
  }, []);
  const openDrawer = useCallback(() => setDrawerOpen(true), []);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);
  const showCharts = useCallback(() => setView({ mode: 'charts' }), []);
  const showMap = useCallback(() => setView({ mode: 'map' }), []);

  return (
    <>
      {mode === 'map' ? (
        <LiveMap onOpenAvailability={openDrawer} onShowCharts={showCharts} notice={hasNotice ? <StateNotice /> : null} />
      ) : (
        <ChartsView onOpenAvailability={openDrawer} onShowMap={showMap} />
      )}
      <AvailabilityDrawer open={drawerOpen} onClose={closeDrawer} />
    </>
  );
}
