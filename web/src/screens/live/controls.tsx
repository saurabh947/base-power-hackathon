// Run controls in the map's bottom-left corner: Pause/Resume, Speed, Reset (with a confirmation) and
// "Crash a controller", which is open to everyone (docs/UX.md, Crashing controllers). A visitor can also
// crash one controller by clicking its gauge in the "Charge by controller" panel, after a small "Crash
// controller 7?" confirmation, and restart a crashed one the same way ("Restart controller 7?", both in
// ControllerPopover). Crashes and restarts are limited to one every 2 s for everyone together, and the server
// never leaves fewer than 2 working controllers (./crash.ts).

import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { Dialog } from '../../components/Dialog.tsx';
import { Icon } from '../../components/Icon.tsx';
import { useLive } from '../../live/liveStore.ts';
import { controllerName } from '../../lib/fleet.ts';
import { fmtCount, fmtSpeed, fmtSpeedShort, plural } from '../../lib/format.ts';
import { pauseRun, resetRun, resumeRun, setSpeed } from './actions.ts';
import {
  ASK_POP_WIDTH,
  crashController,
  restartController,
  useCrashable,
  useCrashWait,
  useRestartWait,
  workerCities,
  type AskTarget,
} from './crash.ts';

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

// ---- crashing a controller (the logic is in ./crash.ts) -----------------------------------------------

/** "Crash a controller": a random working one, for everyone watching. Clicking a controller's gauge picks one. */
export function CrashButton() {
  const { can, why } = useCrashable();
  const { busy, waitS } = useCrashWait();
  const label = busy ? 'Crashing…' : waitS > 0 ? `Next crash in ${waitS} s` : 'Crash a controller';
  return (
    <button
      type="button"
      className="btn btn-danger crash-button"
      onClick={() => void crashController()}
      disabled={!can || busy || waitS > 0}
      title={why ?? 'Crashes a random working controller. To pick one, click its gauge; click a crashed one to restart it.'}
    >
      <Icon name="crash" />
      <span className="crash-button-label">{label}</span>
    </button>
  );
}

/**
 * Just above the gauge that was clicked (render it inside the gauges' wrapper, from useControllerAsk):
 * "Crash controller 7?" for a working controller, "Restart controller 7?" for a crashed one. Esc, Cancel, a
 * click elsewhere or tabbing away closes it.
 */
export function ControllerPopover({ target, onClose }: { target: AskTarget; onClose: (refocus: boolean) => void }) {
  const titleId = useId();
  const ref = useRef<HTMLDivElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const worker = useLive((s) => s.workers.find((w) => w.worker_id === target.workerId));
  // Decided when it opens: a crashed controller is offered a restart, any other a crash.
  const [mode] = useState<'crash' | 'restart'>(() => (worker?.status === 'dead' ? 'restart' : 'crash'));
  const crashable = useCrashable();
  const crashWait = useCrashWait();
  const restartWait = useRestartWait();
  const status = useLive((s) => s.status);
  const restart = mode === 'restart';
  const { busy, waitS } = restart ? restartWait : crashWait;
  const running = status === 'running' || status === 'paused';
  const ready = restart ? worker?.status === 'dead' && running : worker?.status === 'healthy' && crashable.can;
  const allowed = ready && waitS === 0;
  const why = restart ? (running ? null : 'Start a simulation first.') : crashable.why;

  useEffect(() => {
    // The confirm button when it can be pressed, else the popover itself, so Esc and Tab work from here.
    const button = confirmRef.current;
    (button && !button.disabled ? button : ref.current)?.focus();
    const onDown = (e: PointerEvent) => {
      const el = e.target as Node | null;
      if (!ref.current?.contains(el) && !target.button.contains(el)) onClose(false);
    };
    document.addEventListener('pointerdown', onDown);
    return () => document.removeEventListener('pointerdown', onDown);
  }, [onClose, target.button]);

  // Someone else got there first (crashed it, or restarted it): nothing is left to confirm.
  useEffect(() => {
    if (!worker || busy) return;
    if (restart ? worker.status !== 'dead' : worker.status === 'dead') onClose(false);
  }, [worker, busy, restart, onClose]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault(); // the map's own Esc (zoom out, all of Texas) waits for the next press
      e.stopPropagation();
      onClose(true);
    }
  };

  const confirm = async () => {
    if (busy || !allowed) return;
    const done = restart ? await restartController(target.workerId) : await crashController(target.workerId);
    if (done) onClose(true);
  };

  const name = controllerName(target.workerId);
  const lower = name.replace(/^Controller/, 'controller');
  const cities = workerCities(worker);
  const count = worker?.batteries ?? 0;
  let text: string;
  if (restart) text = 'It takes its own batteries back from the other controllers, with whatever charge they have by then.';
  else if (worker?.status === 'healthy') {
    text = `Its ${fmtCount(count)} ${plural(count, 'battery', 'batteries')}${cities ? ` in ${cities}` : ''} lose their charge and pulse red on the map until the other controllers take them over. It stays down until someone restarts it.`;
  } else text = `${name} is not working right now, so it can't be crashed.`;
  return (
    // oxlint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- Esc closes this small dialog, as in native ones
    <div
      ref={ref}
      className={`crash-pop${restart ? ' is-restart' : ''}`}
      role="dialog"
      aria-labelledby={titleId}
      tabIndex={-1}
      style={{ left: target.left, bottom: target.bottom, width: ASK_POP_WIDTH }}
      onKeyDown={onKeyDown}
      onBlur={(e) => {
        const next = e.relatedTarget as Node | null;
        // Tabbing out closes it; a click elsewhere is handled by the pointerdown listener.
        if (next && !ref.current?.contains(next) && next !== target.button) onClose(false);
      }}
    >
      <p className="crash-pop-title" id={titleId}>
        {restart ? 'Restart' : 'Crash'} {lower}?
      </p>
      <p className="crash-pop-text">{text}</p>
      <div className="crash-pop-actions">
        <button
          type="button"
          ref={confirmRef}
          className={`btn btn-sm ${restart ? 'btn-primary' : 'btn-danger'}`}
          onClick={() => void confirm()}
          disabled={!allowed}
          aria-disabled={busy || undefined}
          title={why ?? undefined}
        >
          <Icon name={restart ? 'reset' : 'crash'} size={15} />
          {busy ? (restart ? 'Restarting…' : 'Crashing…') : waitS > 0 ? `Wait ${waitS} s` : restart ? 'Restart it' : 'Crash it'}
        </button>
        <button type="button" className="btn btn-quiet btn-sm" onClick={() => onClose(true)}>
          Cancel
        </button>
      </div>
    </div>
  );
}
