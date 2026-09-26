import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { api } from '../../api/client.ts';
import type { AvailabilityRequest, AvailabilityResponse } from '../../api/types.ts';
import { Dialog } from '../../components/Dialog.tsx';
import { Icon } from '../../components/Icon.tsx';
import { HowItWorks } from '../../components/Panel.tsx';
import { TechOnly } from '../../components/Tech.tsx';
import { liveStore, resync, useLive } from '../../live/liveStore.ts';
import { byBatteries, useCities } from '../../lib/cities.ts';
import { controllerName } from '../../lib/fleet.ts';
import { areaName, fmtCount, fmtNumber, fmtPercent, fmtTimeOfDay, plural } from '../../lib/format.ts';
import { showToast } from '../../state/toasts.ts';
import { runAction } from './actions.ts';

async function applyChange(change: AvailabilityRequest, describe: (r: AvailabilityResponse) => string) {
  return runAction(
    () => api.availability(change),
    'availability',
    (r) => {
      // With no run, the server sends idle messages without battery_status: fetch the state so the map redraws.
      const status = liveStore.get().status;
      if (status === 'idle' || status === null) void resync();
      showToast({
        kind: 'success',
        title: describe(r),
        detail: `${fmtCount(r.available)} of ${fmtCount(r.total)} batteries online${
          r.applied_from_tick_utc ? `, from ${fmtTimeOfDay(r.applied_from_tick_utc)} market time` : ''
        }.`,
        technical: JSON.stringify(change),
      });
    },
  );
}

function sendAreaChange(location: string, percent: number) {
  void applyChange({ scope: 'location', location, offline_fraction: percent / 100 }, (r) =>
    percent === 0
      ? `All ${areaName(location)} batteries are back online.`
      : `${fmtPercent(percent / 100)} of ${areaName(location)} batteries are offline (${fmtCount(r.affected)}).`,
  );
}

interface Debounce {
  timer: ReturnType<typeof setTimeout> | null;
  pending: number | null;
}

function AreaSlider({ location, startAtZero }: { location: string; startAtZero: boolean }) {
  const id = useId();
  const counts = useLive((s) => s.availability?.by_location?.[location] ?? null);
  // Start from what is offline now (the next frame may not reflect a change just made, hence startAtZero).
  const [percent, setPercent] = useState<number>(() =>
    !startAtZero && counts && counts.total ? Math.round(((1 - counts.available / counts.total) * 100) / 5) * 5 : 0,
  );
  const debounce = useRef<Debounce>({ timer: null, pending: null });

  // Send the last value once the slider has been still for a moment (the API allows 10 changes a second
  // for everyone together); if the drawer closes first, send it right away.
  useEffect(() => {
    const d = debounce.current;
    return () => {
      if (d.timer) clearTimeout(d.timer);
      if (d.pending !== null) sendAreaChange(location, d.pending);
      d.pending = null;
    };
  }, [location]);

  const onChange = (value: number) => {
    const d = debounce.current;
    setPercent(value);
    d.pending = value;
    if (d.timer) clearTimeout(d.timer);
    d.timer = setTimeout(() => {
      d.pending = null;
      sendAreaChange(location, value);
    }, 450);
  };

  return (
    <div className="area-slider">
      <div className="area-slider-top">
        <label htmlFor={id}>
          <strong>{areaName(location)}</strong>
        </label>
        <span className="num">{fmtPercent(percent / 100)} offline</span>
      </div>
      <input
        id={id}
        type="range"
        min={0}
        max={100}
        step={5}
        value={percent}
        aria-valuetext={`${fmtPercent(percent / 100)} offline`}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      <span className="field-hint">
        {counts ? `${fmtCount(counts.available)} of ${fmtCount(counts.total)} batteries online now` : 'No data yet'}
        <TechOnly>
          <span className="tech"> · {location}, offline_fraction {fmtNumber(percent / 100, 2)}</span>
        </TechOnly>
      </span>
    </div>
  );
}

function ControllerOffline() {
  const workers = useLive((s) => s.workers);
  const offline = useLive((s) => s.availability?.offline_workers ?? null);
  const [choice, setChoice] = useState('');
  const [busy, setBusy] = useState(false);
  const selectId = useId();
  const offlineSet = new Set(offline ?? []);
  const candidates = workers.filter((w) => w.status !== 'dead' && !offlineSet.has(w.worker_id));

  const set = async (workerId: number, available: boolean) => {
    setBusy(true);
    await applyChange({ scope: 'worker', worker_id: workerId, available }, (r) =>
      available
        ? `${controllerName(workerId)}'s batteries are back online.`
        : `${controllerName(workerId)}'s ${fmtCount(r.affected)} batteries are offline. The controller keeps running.`,
    );
    setBusy(false);
    setChoice('');
  };

  return (
    <>
      <form
        className="inline-form"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          if (choice) void set(Number(choice), false);
        }}
      >
        <div className="field">
          <label className="field-label" htmlFor={selectId}>
            Controller
          </label>
          <select id={selectId} value={choice} onChange={(e) => setChoice(e.target.value)}>
            <option value="">Choose a controller…</option>
            {candidates.map((w) => (
              <option key={w.worker_id} value={String(w.worker_id)}>
                {controllerName(w.worker_id)} ({fmtCount(w.batteries)} {plural(w.batteries, 'battery', 'batteries')})
              </option>
            ))}
          </select>
        </div>
        <button type="submit" className="btn" disabled={!choice || busy}>
          Take its batteries offline
        </button>
      </form>
      {offline && offline.length ? (
        <ul className="offline-list" aria-label="Controllers with batteries offline">
          {offline.map((id) => (
            <li key={id}>
              <span>
                <Icon name="plug" size={16} /> {controllerName(id)}: batteries offline
              </span>
              <button type="button" className="btn btn-sm" onClick={() => void set(id, true)} disabled={busy}>
                Bring back online
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

/**
 * The "Take batteries offline" drawer. It opens on the left, over West Texas, with a light backdrop, so the
 * cities stay in view and their dots can be watched turning red while the sliders move.
 */
export function AvailabilityDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const availability = useLive((s) => s.availability);
  const cities = useCities();
  // Bumped by "Bring every battery back online" so the sliders remount at 0%; cleared when the drawer closes.
  const [allBackCount, setAllBackCount] = useState(0);
  const [busy, setBusy] = useState(false);
  const allOnline = !!availability && availability.available >= availability.total && availability.offline_workers.length === 0;
  const allBack = async () => {
    setBusy(true);
    const ok = await applyChange({ scope: 'all', available: true }, () => 'Every battery is back online.');
    setBusy(false);
    if (ok) setAllBackCount((k) => k + 1);
  };
  const close = () => {
    setAllBackCount(0);
    onClose();
  };
  return (
    <Dialog open={open} onClose={close} title="Take batteries offline" variant="drawer" side="left" backdrop="light">
      <p className="panel-what">
        Offline batteries can't charge or sell, so the fleet's plan shrinks. On the map they show as red rings, where
        they are. This is different from crashing a controller.
      </p>

      <div className="drawer-top">
        <p>
          {availability ? (
            <>
              <strong className="num">{fmtCount(availability.available)}</strong> of {fmtCount(availability.total)}{' '}
              batteries are online.
            </>
          ) : (
            'No battery data yet.'
          )}
        </p>
        <button type="button" className="btn" onClick={() => void allBack()} disabled={busy || allOnline}>
          <Icon name="reset" />
          {allOnline ? 'Every battery is online' : 'Bring every battery back online'}
        </button>
      </div>

      <HowItWorks>
        <p>
          Changes show on the map within a second, count from the next 5-minute step and affect everyone watching.
          Taking a share of an area offline always picks the same batteries for the same share. A controller whose
          batteries are offline keeps running; it just has nothing to do.
        </p>
      </HowItWorks>

      <section className="drawer-section" key={allBackCount}>
        <h3>By area</h3>
        {byBatteries(cities).map((c) => (
          <AreaSlider key={c.location} location={c.location} startAtZero={allBackCount > 0} />
        ))}
      </section>

      <section className="drawer-section">
        <h3>Take a controller's batteries offline</h3>
        <ControllerOffline />
      </section>
    </Dialog>
  );
}
