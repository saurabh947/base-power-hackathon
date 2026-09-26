import { useState } from 'react';
import { api, ApiError } from '../../api/client.ts';
import { friendlyError } from '../../api/errors.ts';
import type { KillResponse } from '../../api/types.ts';
import { Dialog } from '../../components/Dialog.tsx';
import { Icon } from '../../components/Icon.tsx';
import { TechOnly } from '../../components/Tech.tsx';
import { reconnectNow, useConnection, useLive } from '../../live/liveStore.ts';
import { fmtClock, fmtDate, fmtSpeedShort, fmtSpeed, localDateOf, fmtLocalDate } from '../../lib/format.ts';
import { controllerName } from '../../lib/fleet.ts';
import { presenterPasscode, useIsPresenter, leavePresenter } from '../../state/presenter.ts';
import { showToast } from '../../state/toasts.ts';
import { pauseRun, resetRun, resumeRun, setSpeed } from './actions.ts';

const SPEEDS = [1, 2, 5, 10];

function Clock() {
  const simTime = useLive((s) => s.simTimeUtc);
  const status = useLive((s) => s.status);
  if (!simTime) {
    return <div className="clock">{status === 'idle' ? 'No simulation running' : 'Replay clock'}</div>;
  }
  // fmtClock gives "May 16, 2025, 7:45 pm, Houston time"; when space is tight, "Houston time" moves
  // to the next line as a whole instead of being cut off.
  const [when, zone] = fmtClock(simTime).split(/, (?=Houston time$)/);
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
function ReplayLabel() {
  const startUtc = useLive((s) => s.settings?.start_utc ?? null);
  const firstTick = useLive((s) => s.history[0]?.ts_utc ?? null);
  const source = startUtc ?? firstTick;
  if (!source) return null;
  const day = localDateOf(source);
  return <span>Replaying real prices from {day ? fmtLocalDate(day) : fmtDate(source)}</span>;
}

function RunStatus() {
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

function ConnectionStatus() {
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

function TechCounters() {
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

function PauseResume() {
  const status = useLive((s) => s.status);
  const [busy, setBusy] = useState(false);
  const paused = status === 'paused';
  const can = status === 'running' || status === 'paused';
  const onClick = async () => {
    setBusy(true);
    await (paused ? resumeRun() : pauseRun());
    setBusy(false);
  };
  return (
    <button
      type="button"
      className={`btn ${paused ? 'btn-primary' : ''}`}
      onClick={() => void onClick()}
      disabled={!can || busy}
    >
      <Icon name={paused ? 'play' : 'pause'} />
      {paused ? 'Resume' : 'Pause'}
    </button>
  );
}

function SpeedSelect() {
  const speed = useLive((s) => s.speedTps);
  const status = useLive((s) => s.status);
  const can = status === 'running' || status === 'paused' || status === 'starting';
  const options = speed !== null && !SPEEDS.includes(speed) ? [...SPEEDS, speed].toSorted((a, b) => a - b) : SPEEDS;
  return (
    <span className="speed-control">
      <label htmlFor="speed-select">Speed</label>
      <select
        id="speed-select"
        value={speed === null ? '' : String(speed)}
        disabled={!can}
        onChange={(e) => void setSpeed(Number(e.target.value))}
        aria-describedby="speed-help"
      >
        {speed === null ? <option value="">–</option> : null}
        {options.map((v) => (
          <option key={v} value={String(v)} title={fmtSpeed(v)}>
            {fmtSpeedShort(v)}
          </option>
        ))}
      </select>
      <span id="speed-help" className="sr-only">
        How much market time passes per second: for example, 1 hour every 6 seconds.
      </span>
    </span>
  );
}

function ResetButton() {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const status = useLive((s) => s.status);
  const confirm = async () => {
    setBusy(true);
    const ok = await resetRun();
    setBusy(false);
    if (ok !== null) setOpen(false);
  };
  return (
    <>
      <button type="button" className="btn" onClick={() => setOpen(true)} disabled={status === null}>
        <Icon name="reset" />
        Reset
      </button>
      <Dialog open={open} onClose={() => setOpen(false)} title="Reset the simulation for everyone?" initialFocusSelector="[data-autofocus]">
        <p>
          Reset stops the replay for everyone watching, restarts all 20 controllers (including crashed ones) and brings
          every battery back online.
        </p>
        <div className="editor-actions">
          <button type="button" className="btn btn-danger" onClick={() => void confirm()} disabled={busy}>
            {busy ? 'Resetting…' : 'Reset for everyone'}
          </button>
          <button type="button" className="btn btn-quiet" onClick={() => setOpen(false)} data-autofocus>
            Keep it running
          </button>
        </div>
      </Dialog>
    </>
  );
}

function CrashControl() {
  const workers = useLive((s) => s.workers);
  const status = useLive((s) => s.status);
  const [target, setTarget] = useState('random');
  const [busy, setBusy] = useState(false);
  const can = status === 'running' || status === 'paused';

  const crash = async () => {
    const passcode = presenterPasscode();
    if (!passcode) return;
    setBusy(true);
    const workerId = target === 'random' ? undefined : Number(target);
    let result: KillResponse | null = null;
    try {
      result = await api.kill(passcode, workerId);
    } catch (err) {
      const f = friendlyError(err, 'kill');
      // A rejected passcode is useless from now on: drop it so the presenter can unlock again.
      if (err instanceof ApiError && err.status === 403) leavePresenter();
      showToast({ kind: 'error', title: f.title, detail: f.detail, technical: f.technical });
    }
    setBusy(false);
    if (result) {
      showToast({
        kind: 'success',
        title: `${controllerName(result.worker_id)} crashed.`,
        detail: 'Watch the others take over its batteries, and the power gap close.',
        technical: `worker_id ${result.worker_id}, pid ${result.pid}, killed at ${result.killed_at_utc}`,
      });
      setTarget('random');
    }
  };

  // Presenter only. "Leave presenter mode" lives in the footer.
  return (
    <span className="crash-control">
      <label className="sr-only" htmlFor="crash-target">
        Which controller to crash
      </label>
      <select id="crash-target" value={target} onChange={(e) => setTarget(e.target.value)} disabled={!can}>
        <option value="random">Random</option>
        {workers
          .filter((w) => w.status !== 'dead')
          .map((w) => (
            <option key={w.worker_id} value={String(w.worker_id)}>
              {controllerName(w.worker_id)}
            </option>
          ))}
      </select>
      <button type="button" className="btn btn-danger" onClick={() => void crash()} disabled={!can || busy}>
        <Icon name="crash" />
        {busy ? 'Crashing…' : 'Crash a controller'}
      </button>
    </span>
  );
}

export function TopBar({ onOpenAvailability }: { onOpenAvailability: () => void }) {
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
