import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { api } from '../../api/client.ts';
import { friendlyError, type FriendlyError } from '../../api/errors.ts';
import type { AvailabilityRequest, AvailabilityResponse, BatteryInfo } from '../../api/types.ts';
import { Dialog } from '../../components/Dialog.tsx';
import { Icon } from '../../components/Icon.tsx';
import { ErrorNotice } from '../../components/Notice.tsx';
import { HowItWorks } from '../../components/Panel.tsx';
import { TechOnly } from '../../components/Tech.tsx';
import { liveStore, resync, useLive } from '../../live/liveStore.ts';
import { byBatteries, useCities } from '../../lib/cities.ts';
import {
  areaOfBattery,
  batteryId as batteryIdOf,
  batteryIndex,
  batteryRanges,
  controllerName,
  DEFAULT_FLEET_SIZE,
  DEFAULT_SHARES,
  parseBatteryInput,
} from '../../lib/fleet.ts';
import {
  areaName,
  fmtCount,
  fmtNumber,
  fmtPercent,
  fmtPowerFlow,
  fmtTimeOfDay,
  plural,
} from '../../lib/format.ts';
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

/** Battery ids are fleet-wide b00000–b19999 (docs/API.md); each area holds a consecutive block. */
function FindBattery() {
  const fleetSize = useLive((s) => s.settings?.fleet_size ?? DEFAULT_FLEET_SIZE);
  const shares = useLive((s) => s.settings?.locations ?? DEFAULT_SHARES);
  const [text, setText] = useState('');
  const [battery, setBattery] = useState<BatteryInfo | null>(null);
  const [error, setError] = useState<FriendlyError | null>(null);
  const [busy, setBusy] = useState(false);
  const ids = { input: useId(), hint: useId() };

  const ranges = batteryRanges(fleetSize, shares);
  const lastId = batteryIdOf(fleetSize - 1);
  const batteryId = parseBatteryInput(text, fleetSize);
  const index = batteryId ? batteryIndex(batteryId) : null;
  const area = index === null ? null : areaOfBattery(index, ranges);
  let hint = `Type a number from 0 to ${fmtCount(fleetSize - 1)}, or an ID like b00123. `;
  hint += ranges.map((r) => `${areaName(r.location)}: ${batteryIdOf(r.first)}–${batteryIdOf(r.last)}`).join('; ') + '.';
  if (text.trim() && !batteryId) hint = `That is not a battery in this fleet. IDs run from b00000 to ${lastId}.`;
  else if (batteryId) hint = `Looks up ${batteryId}${area ? `, in the ${areaName(area)}` : ''}.`;

  const load = async (id: string) => {
    setBusy(true);
    setError(null);
    try {
      setBattery(await api.battery(id));
    } catch (err) {
      setBattery(null);
      setError(friendlyError(err, 'battery'));
    } finally {
      setBusy(false);
    }
  };

  const toggle = async () => {
    if (!battery) return;
    const available = !battery.available;
    const ok = await applyChange({ scope: 'battery', battery_id: battery.battery_id, available }, () =>
      available ? `Battery ${battery.battery_id} is back online.` : `Battery ${battery.battery_id} is offline.`,
    );
    if (ok) void load(battery.battery_id);
  };

  return (
    <>
      <form
        className="inline-form"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          if (batteryId) void load(batteryId);
        }}
      >
        <div className="field">
          <label className="field-label" htmlFor={ids.input}>
            Battery number or ID
          </label>
          <input
            id={ids.input}
            type="text"
            autoComplete="off"
            spellCheck={false}
            placeholder="e.g. 123 or b00123"
            aria-describedby={ids.hint}
            aria-invalid={text.trim() !== '' && !batteryId}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </div>
        <button type="submit" className="btn" disabled={!batteryId || busy}>
          <Icon name="search" />
          Find
        </button>
      </form>
      <span className="field-hint" id={ids.hint}>
        {hint}
      </span>
      {error ? <ErrorNotice error={error} /> : null}
      {battery ? (
        <div className="battery-card" aria-live="polite">
          <strong>Battery {battery.battery_id}</strong>
          <dl>
            <dt>Area</dt>
            <dd>
              {battery.city ? `${battery.city} area` : areaName(battery.location)}
              <TechOnly>
                <span className="tech">
                  {' '}
                  ({battery.location}
                  {battery.lonlat ? `, ${battery.lonlat[0].toFixed(3)}, ${battery.lonlat[1].toFixed(3)}` : ''})
                </span>
              </TechOnly>
            </dd>
            <dt>Controller</dt>
            <dd>{controllerName(battery.worker_id)}</dd>
            <dt>Size</dt>
            <dd>
              {fmtNumber(battery.power_kw, 1)} kW, {fmtNumber(battery.capacity_kwh, 1)} kWh
            </dd>
            <dt>Charge level</dt>
            <dd>{fmtPercent(battery.charge_level)}</dd>
            <dt>Right now</dt>
            <dd>{fmtPowerFlow(battery.power_kw_now, 'kW')}</dd>
            <dt>Status</dt>
            <dd>{battery.available ? 'Online' : 'Offline'}</dd>
          </dl>
          <div>
            <button type="button" className="btn btn-sm" onClick={() => void toggle()}>
              {battery.available ? 'Take this battery offline' : 'Bring it back online'}
            </button>
          </div>
        </div>
      ) : null}
    </>
  );
}

export function AvailabilityDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const availability = useLive((s) => s.availability);
  const cities = useCities();
  // Bumped by "Bring every battery back online" so the sliders remount at 0%; cleared when the drawer closes.
  const [allBackCount, setAllBackCount] = useState(0);
  const allBack = async () => {
    const ok = await applyChange({ scope: 'all', available: true }, () => 'Every battery is back online.');
    if (ok) setAllBackCount((k) => k + 1);
  };
  const close = () => {
    setAllBackCount(0);
    onClose();
  };
  return (
    <Dialog open={open} onClose={close} title="Take batteries offline" variant="drawer">
      <p className="panel-what">
        Offline batteries can't charge or sell, so the fleet's plan shrinks. This is different from crashing a
        controller.
      </p>
      <HowItWorks>
        <p>
          Changes apply from the next 5-minute step and affect everyone watching. Taking a share of an area offline
          always picks the same batteries for the same share. A controller whose batteries are offline keeps running;
          it just has nothing to do.
        </p>
      </HowItWorks>
      {availability ? (
        <p>
          <strong className="num">{fmtCount(availability.available)}</strong> of {fmtCount(availability.total)} batteries
          are online.
        </p>
      ) : null}

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

      <section className="drawer-section">
        <h3>Find a battery</h3>
        <FindBattery />
      </section>

      <section className="drawer-section">
        <button type="button" className="btn" onClick={() => void allBack()}>
          <Icon name="reset" />
          Bring every battery back online
        </button>
      </section>
    </Dialog>
  );
}
