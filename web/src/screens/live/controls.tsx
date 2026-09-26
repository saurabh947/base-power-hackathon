// Run controls shared by the map's bottom-left corner and the detailed layout's
// top bar: Pause/Resume, Speed, Reset (with a confirmation) and the
// presenter-only "Crash a controller".

import { useId, useState } from 'react';
import { api, ApiError } from '../../api/client.ts';
import { friendlyError } from '../../api/errors.ts';
import type { KillResponse } from '../../api/types.ts';
import { Dialog } from '../../components/Dialog.tsx';
import { Icon } from '../../components/Icon.tsx';
import { useLive } from '../../live/liveStore.ts';
import { controllerName } from '../../lib/fleet.ts';
import { fmtSpeed, fmtSpeedShort } from '../../lib/format.ts';
import { leavePresenter, presenterPasscode } from '../../state/presenter.ts';
import { showToast } from '../../state/toasts.ts';
import { pauseRun, resetRun, resumeRun, setSpeed } from './actions.ts';

const SPEEDS = [1, 2, 5, 10];

export function PauseResume() {
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

export function SpeedSelect() {
  const speed = useLive((s) => s.speedTps);
  const status = useLive((s) => s.status);
  const id = useId();
  const can = status === 'running' || status === 'paused' || status === 'starting';
  const options = speed !== null && !SPEEDS.includes(speed) ? [...SPEEDS, speed].toSorted((a, b) => a - b) : SPEEDS;
  return (
    <span className="speed-control">
      <label htmlFor={`${id}-speed`}>Speed</label>
      <select
        id={`${id}-speed`}
        value={speed === null ? '' : String(speed)}
        disabled={!can}
        onChange={(e) => void setSpeed(Number(e.target.value))}
        aria-describedby={`${id}-help`}
      >
        {speed === null ? <option value="">–</option> : null}
        {options.map((v) => (
          <option key={v} value={String(v)} title={fmtSpeed(v)}>
            {fmtSpeedShort(v)}
          </option>
        ))}
      </select>
      <span id={`${id}-help`} className="sr-only">
        How much market time passes per second: for example, 1 hour every 6 seconds.
      </span>
    </span>
  );
}

export function ResetButton() {
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

/** Presenter only. "Presenter mode" and "Leave presenter mode" are the link in the header. */
export function CrashControl() {
  const workers = useLive((s) => s.workers);
  const status = useLive((s) => s.status);
  const id = useId();
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
        detail: 'Watch the other controllers take over its batteries, and the power gap close.',
        technical: `worker_id ${result.worker_id}, pid ${result.pid}, killed at ${result.killed_at_utc}`,
      });
      setTarget('random');
    }
  };

  return (
    <span className="crash-control">
      <label className="sr-only" htmlFor={`${id}-crash`}>
        Which controller to crash
      </label>
      <select id={`${id}-crash`} value={target} onChange={(e) => setTarget(e.target.value)} disabled={!can}>
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
