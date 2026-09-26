// Run controls shared by the map's bottom-left corner and the detailed layout's top bar: Pause/Resume,
// Speed, Reset (with a confirmation) and "Crash a controller", which is open to everyone (docs/UX.md,
// Crashing controllers). A visitor can also crash one controller by clicking its tile, after a small
// "Crash controller 7?" confirmation (CrashPopover). Crashes are limited to one every 2 s for everyone
// together, and the server never leaves fewer than 2 working controllers (./crash.ts).

import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { Dialog } from '../../components/Dialog.tsx';
import { Icon } from '../../components/Icon.tsx';
import { useLive } from '../../live/liveStore.ts';
import { controllerName } from '../../lib/fleet.ts';
import { fmtCount, fmtSpeed, fmtSpeedShort, plural } from '../../lib/format.ts';
import { pauseRun, resetRun, resumeRun, setSpeed } from './actions.ts';
import { crashController, CRASH_POP_WIDTH, useCrashable, useCrashWait, workerCities, type CrashTarget } from './crash.ts';

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

/** "Crash a controller": a random working one, for everyone watching. Clicking a controller tile picks one. */
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
      title={why ?? 'Crashes a random working controller. To pick one, click its tile.'}
    >
      <Icon name="crash" />
      <span className="crash-button-label">{label}</span>
    </button>
  );
}

/**
 * "Crash controller 7?", just above the tile that was clicked (render it inside the tiles' wrapper, from
 * useCrashAsk). Esc, Cancel, a click elsewhere or tabbing away closes it.
 */
export function CrashPopover({ target, onClose }: { target: CrashTarget; onClose: (refocus: boolean) => void }) {
  const titleId = useId();
  const ref = useRef<HTMLDivElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const worker = useLive((s) => s.workers.find((w) => w.worker_id === target.workerId));
  const { can, why } = useCrashable();
  const { busy, waitS } = useCrashWait();
  const working = worker?.status === 'healthy';
  const allowed = working && can && waitS === 0;

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

  // The controller stopped working meanwhile (someone else crashed it): nothing is left to confirm.
  useEffect(() => {
    if (worker && worker.status === 'dead' && !busy) onClose(false);
  }, [worker, busy, onClose]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault(); // the map's own Esc (zoom out, all of Texas) waits for the next press
      e.stopPropagation();
      onClose(true);
    }
  };

  const confirm = async () => {
    if (busy || !allowed) return;
    if (await crashController(target.workerId)) onClose(true);
  };

  const name = controllerName(target.workerId);
  const cities = workerCities(worker);
  const count = worker?.batteries ?? 0;
  return (
    // oxlint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- Esc closes this small dialog, as in native ones
    <div
      ref={ref}
      className="crash-pop"
      role="dialog"
      aria-labelledby={titleId}
      tabIndex={-1}
      style={{ left: target.left, bottom: target.bottom, width: CRASH_POP_WIDTH }}
      onKeyDown={onKeyDown}
      onBlur={(e) => {
        const next = e.relatedTarget as Node | null;
        // Tabbing out closes it; a click elsewhere is handled by the pointerdown listener.
        if (next && !ref.current?.contains(next) && next !== target.button) onClose(false);
      }}
    >
      <p className="crash-pop-title" id={titleId}>
        Crash {name.replace(/^Controller/, 'controller')}?
      </p>
      <p className="crash-pop-text">
        {working
          ? `Its ${fmtCount(count)} ${plural(count, 'battery', 'batteries')}${cities ? ` in ${cities}` : ''} pulse red on the map until the other controllers take them over.`
          : `${name} is not working right now, so it can't be crashed.`}
      </p>
      <div className="crash-pop-actions">
        <button
          type="button"
          ref={confirmRef}
          className="btn btn-danger btn-sm"
          onClick={() => void confirm()}
          disabled={!allowed}
          aria-disabled={busy || undefined}
          title={why ?? undefined}
        >
          <Icon name="crash" size={15} />
          {busy ? 'Crashing…' : waitS > 0 ? `Wait ${waitS} s` : 'Crash it'}
        </button>
        <button type="button" className="btn btn-quiet btn-sm" onClick={() => onClose(true)}>
          Cancel
        </button>
      </div>
    </div>
  );
}
