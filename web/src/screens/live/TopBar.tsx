import { Icon } from '../../components/Icon.tsx';
import { TechOnly } from '../../components/Tech.tsx';
import { reconnectNow, useConnection, useLive } from '../../live/liveStore.ts';
import { fmtClock, fmtDate, localDateOf, fmtLocalDate } from '../../lib/format.ts';
import { useIsPresenter } from '../../state/presenter.ts';
import { CrashControl, PauseResume, ResetButton, SpeedSelect } from './controls.tsx';

export function Clock() {
  const simTime = useLive((s) => s.simTimeUtc);
  const status = useLive((s) => s.status);
  if (!simTime) {
    return <div className="clock">{status === 'idle' ? 'No simulation running' : 'Replay clock'}</div>;
  }
  // fmtClock gives "May 16, 2025, 7:45 pm, Central time"; when space is tight, "Central time" moves
  // to the next line as a whole instead of being cut off.
  const [when, zone] = fmtClock(simTime).split(/, (?=Central time$)/);
  return (
    <div className="clock">
      <span className="sr-only">Replay time: </span>
      <span className="nowrap">{when}</span>
      {zone ? (
        <>
          , <span className="nowrap">{zone}</span>
        </>
      ) : null}
    </div>
  );
}

/** "Replaying real prices from May 16, 2025" — an honest label for what is on screen. */
export function ReplayLabel() {
  const startUtc = useLive((s) => s.settings?.start_utc ?? null);
  const firstTick = useLive((s) => s.history[0]?.ts_utc ?? null);
  const source = startUtc ?? firstTick;
  if (!source) return null;
  const day = localDateOf(source);
  return <span>Replaying real prices from {day ? fmtLocalDate(day) : fmtDate(source)}</span>;
}

export function RunStatus() {
  const status = useLive((s) => s.status);
  const received = useLive((s) => s.received);
  if (!received || !status) return null;
  const map = {
    running: ['dot-ok dot-pulse', 'Running'],
    paused: ['dot-warn', 'Paused'],
    starting: ['dot-warn dot-pulse', 'Starting…'],
    finished: ['', 'Finished'],
    idle: ['', 'Not running'],
  } as const;
  const [dot, label] = map[status];
  return (
    <span className="status-line">
      <span className={`dot ${dot}`} aria-hidden="true" />
      {label}
    </span>
  );
}

export function ConnectionStatus() {
  const mode = useConnection((s) => s.mode);
  const lastError = useConnection((s) => s.lastError);
  let content: [string, string] | null = null;
  if (mode === 'live') content = ['dot-ok', 'Live updates'];
  else if (mode === 'connecting') content = ['dot-warn dot-pulse', 'Connecting…'];
  else if (mode === 'reconnecting') content = ['dot-warn dot-pulse', 'Reconnecting…'];
  else if (mode === 'polling') content = ['dot-warn', 'Updating every 2 s (live connection unavailable)'];
  else if (mode === 'offline') content = ['dot-fail', "Can't reach the simulator. Retrying…"];
  if (!content) return null;
  return (
    <span className="status-line" role="status">
      <span className={`dot ${content[0]}`} aria-hidden="true" />
      {content[1]}
      {mode === 'offline' || mode === 'reconnecting' ? (
        <button type="button" className="linklike" onClick={reconnectNow}>
          Try now
        </button>
      ) : null}
      <TechOnly>{lastError ? <span className="tech">({lastError})</span> : null}</TechOnly>
    </span>
  );
}

export function TechCounters() {
  const messages = useLive((s) => s.messages);
  const ticks = useLive((s) => s.totals.ticks);
  const transport = useConnection((s) => s.transport);
  return (
    <TechOnly>
      <span className="tech">
        {transport ?? 'no transport'} · {messages} messages · tick {ticks}
      </span>
    </TechOnly>
  );
}

export function TopBar({ onOpenAvailability, onShowMap }: { onOpenAvailability: () => void; onShowMap: () => void }) {
  const presenter = useIsPresenter();
  return (
    <div className="live-topbar">
      <div className="page live-topbar-inner">
        <div className="clock-block">
          <Clock />
          <div className="clock-sub">
            <ReplayLabel />
            <RunStatus />
            <ConnectionStatus />
            <TechCounters />
          </div>
        </div>
        <div className="topbar-controls" role="group" aria-label="Simulation controls">
          <button type="button" className="btn" onClick={onShowMap}>
            <Icon name="map" />
            Show map
          </button>
          <PauseResume />
          <SpeedSelect />
          <button type="button" className="btn" onClick={onOpenAvailability}>
            <Icon name="plug" />
            Take batteries offline
          </button>
          <ResetButton />
          {presenter ? <CrashControl /> : null}
        </div>
      </div>
    </div>
  );
}
