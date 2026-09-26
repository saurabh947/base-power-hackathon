// The live map's four corner panels (docs/UX.md, Live map > Corners). Each one
// shows the selected city's numbers, or the whole fleet's for "All of Texas".

import { useState } from 'react';
import type { Tick, Worker, WorkerStatus } from '../../../api/types.ts';
import { AnimatedNumber } from '../../../components/AnimatedNumber.tsx';
import { Icon, type IconName } from '../../../components/Icon.tsx';
import { PresenterDialog } from '../../../components/PresenterDialog.tsx';
import { TechBlock, TechOnly, Term } from '../../../components/Tech.tsx';
import { useLive } from '../../../live/liveStore.ts';
import { byBatteries, cityName, useCities } from '../../../lib/cities.ts';
import { controllerName } from '../../../lib/fleet.ts';
import {
  fmtClock,
  fmtCount,
  fmtEnergy,
  fmtHomesPower,
  fmtNumber,
  fmtPercentValue,
  fmtPower,
  fmtPowerFlow,
  fmtPriceKwh,
  fmtPriceKwhShort,
  fmtPriceMwh,
  fmtTimeOfDay,
  fmtUsd,
  plural,
} from '../../../lib/format.ts';
import { useLayoutState, useMapStatus } from '../../../map/feed.ts';
import { CHARGING, DOWN, OFFLINE, SELLING, WAITING } from '../../../map/status.ts';
import { leavePresenter, useIsPresenter } from '../../../state/presenter.ts';
import { useTechnical } from '../../../state/prefs.ts';
import { showToast } from '../../../state/toasts.ts';
import { ActionChip } from '../ActionChip.tsx';
import { CrashControl, PauseResume, ResetButton, SpeedSelect } from '../controls.tsx';
import { ConnectionStatus, ReplayLabel, RunStatus, TechCounters } from '../TopBar.tsx';
import { MiniPowerChart, MiniPriceChart } from './MiniCharts.tsx';
import { ON_TARGET_PCT, scopeAvailability, scopeTotals, scopeTracking, workerShare, type Scope } from './scope.ts';

const lastTick = (s: { history: Tick[] }) => s.history[s.history.length - 1] ?? null;

/** "Houston" or "All of Texas". */
function scopeLabel(scope: Scope): string {
  return scope === null ? 'All of Texas' : cityName(scope);
}

// ---- top left: clock and price ------------------------------------------------------

function MapClock() {
  const simTime = useLive((s) => s.simTimeUtc);
  const status = useLive((s) => s.status);
  if (!simTime) return <p className="map-clock">{status === 'idle' ? 'No simulation running' : 'Replay clock'}</p>;
  const [when, zone] = fmtClock(simTime).split(/, (?=Houston time$)/);
  return (
    <p className="map-clock">
      <span className="sr-only">Replay time: </span>
      <span className="nowrap">{when}</span>
      {zone ? (
        <>
          {' '}
          <span className="map-clock-zone nowrap">{zone}</span>
        </>
      ) : null}
    </p>
  );
}

function CityPrice({ location }: { location: string }) {
  const tick = useLive(lastTick);
  const hasForecast = useLive((s) => !!s.forecast[location]);
  const price = tick?.price?.[location];
  const decision = tick?.decision_price?.[location];
  return (
    <div className="corner-block">
      <h3 className="corner-label">
        <Term plain={`Price right now · ${cityName(location)}`} tech="RT settlement point price" />
      </h3>
      {typeof price === 'number' ? (
        <>
          <p className="corner-big num">{fmtPriceMwh(price)}</p>
          <p className="corner-line">
            <span className="num">{fmtPriceKwh(price)}</span>
            <ActionChip action={tick?.action?.[location]} />
          </p>
        </>
      ) : (
        <p className="corner-line muted">No price yet.</p>
      )}
      <MiniPriceChart location={location} />
      <ul className="mini-key" aria-label="Chart key">
        <li>
          <span className="key-swatch key-line" aria-hidden="true" />
          Real price, last 6 h
        </li>
        <li>
          <span className="key-swatch key-band" aria-hidden="true" />
          {hasForecast ? 'AI forecast, likely range' : 'AI forecast (none yet)'}
        </li>
      </ul>
      <TechOnly>
        <p className="tech">
          {location}
          {typeof decision === 'number' ? ` · acted on ${fmtPriceMwh(decision)} (15 min late)` : ''}
        </p>
      </TechOnly>
    </div>
  );
}

function AllPrices({ onSelect }: { onSelect: (location: string) => void }) {
  const tick = useLive(lastTick);
  const cities = useCities();
  return (
    <div className="corner-block">
      <h3 className="corner-label">
        <Term plain="Electricity price right now" tech="RT settlement point price" />
      </h3>
      <ul className="price-rows">
        {byBatteries(cities).map((c) => {
          const price = tick?.price?.[c.location];
          const action = tick?.action?.[c.location];
          return (
            <li key={c.location}>
              <button type="button" className="price-row" onClick={() => onSelect(c.location)}>
                <span className="price-row-name">{c.name}</span>
                {typeof price === 'number' ? (
                  <span className="price-row-value">
                    <strong className="num">{fmtPriceMwh(price)}</strong>
                    <span className="num muted">{fmtPriceKwhShort(price)}</span>
                  </span>
                ) : (
                  <span className="price-row-value muted">
                    –<span className="sr-only"> no price yet</span>
                  </span>
                )}
                <ActionIcon action={action} />
                <span className="sr-only">. Show {c.name} on the map.</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

const ACTION_ICON = { charge: 'down', sell: 'up', hold: 'minus' } as const;
const ACTION_WORD = { charge: 'Charging', sell: 'Selling', hold: 'Waiting' } as const;

/** An arrow (▲ selling, ▼ charging, — waiting) coloured like the action chips; the word is in the tooltip and for screen readers. */
function ActionIcon({ action }: { action: Tick['action'][string] }) {
  if (!action || !(action in ACTION_ICON)) return <span className="action-mini muted" aria-hidden="true">–</span>;
  return (
    <span className={`action-mini action-${action}`} title={ACTION_WORD[action]}>
      <Icon name={ACTION_ICON[action]} size={15} />
      <span className="sr-only">{ACTION_WORD[action]}</span>
    </span>
  );
}

export function ClockCorner({ scope, onSelect }: { scope: Scope; onSelect: (location: string) => void }) {
  return (
    <section className="corner corner-tl" aria-label="Replay clock and electricity price">
      <div className="corner-status">
        <RunStatus />
        <ConnectionStatus />
      </div>
      <MapClock />
      <p className="corner-sub">
        <ReplayLabel />
      </p>
      <TechCounters />
      <hr className="corner-rule" />
      {scope === null ? <AllPrices onSelect={onSelect} /> : <CityPrice location={scope} />}
    </section>
  );
}

// ---- top right: money and power ------------------------------------------------------

const formatMoney = (n: number) => fmtUsd(n, 2);

/** Average battery power, for a city's "on target" floor (0.5% of its nameplate). */
function useAvgPowerKw(): number {
  return useLive((s) => {
    const r = s.settings?.power_kw_range;
    return r ? (r[0] + r[1]) / 2 : 8.25;
  });
}

export function MoneyCorner({ scope }: { scope: Scope }) {
  const totals = useLive((s) => s.totals);
  const received = useLive((s) => s.received);
  const tick = useLive(lastTick);
  const cities = useCities();
  const avgKw = useAvgPowerKw();
  const technical = useTechnical();
  const money = scopeTotals(totals, scope);
  const batteries = scope === null ? cities.reduce((a, c) => a + c.batteries, 0) : (cities.find((c) => c.location === scope)?.batteries ?? 0);
  const track = scopeTracking(tick, scope, batteries, avgKw);
  const onTarget = track.errorPct !== null ? track.errorPct <= ON_TARGET_PCT : null;
  const gap = track.target !== null && track.actual !== null ? Math.abs(track.target - track.actual) : null;
  const where = scope === null ? 'in this replay' : `in ${cityName(scope)}`;

  return (
    <section className="corner corner-tr" aria-label={`Money and power, ${scopeLabel(scope)}`}>
      <h2 className="corner-label">Earned so far {where}</h2>
      <p className="corner-money num">
        {received && money ? (
          <>
            <span aria-hidden="true">
              <AnimatedNumber value={money.revenue_usd} format={formatMoney} />
            </span>
            <span className="sr-only">{fmtUsd(money.revenue_usd, 2)}</span>
          </>
        ) : (
          <span className="muted">–</span>
        )}
      </p>
      <p className="corner-sub">
        {money ? (
          <>
            Gross wholesale value. Sold <strong>{fmtEnergy(money.sold_kwh)}</strong> · Bought{' '}
            <strong>{fmtEnergy(money.bought_kwh)}</strong>
          </>
        ) : (
          'This simulator does not report money per city.'
        )}
      </p>
      <hr className="corner-rule" />
      <h3 className="corner-label">Power: asked for vs delivered</h3>
      {tick ? (
        <>
          <dl className="corner-pairs">
            <dt>
              <span className="key-swatch key-asked" aria-hidden="true" />
              <Term plain="Asked for" tech="target kW" />
            </dt>
            <dd className="num">{track.target === null ? '–' : fmtPowerFlow(track.target)}</dd>
            <dt>
              <span className="key-swatch key-delivered" aria-hidden="true" />
              <Term plain="Delivered" tech="actual kW" />
            </dt>
            <dd className="num">{track.actual === null ? '–' : fmtPowerFlow(track.actual)}</dd>
          </dl>
          <MiniPowerChart scope={scope} />
          <p className="corner-line">
            {onTarget === null ? null : onTarget ? (
              <span className="chip chip-ok">
                <Icon name="check" size={13} />
                On target
              </span>
            ) : (
              <span className="chip chip-warn">
                <Icon name="alert" size={13} />
                Catching up{gap !== null ? `: ${fmtPower(gap)} short` : ''}
              </span>
            )}
            {track.target !== null && Math.abs(track.target) >= 1 ? (
              <span className="muted small">{fmtHomesPower(track.target)}</span>
            ) : null}
          </p>
          {technical ? (
            <TechBlock
              rows={[
                ['error_pct', track.errorPct === null ? '–' : `${fmtPercentValue(track.errorPct)} (target: within ${ON_TARGET_PCT}%)${scope ? ', computed per city' : ''}`],
                ['target_kw', track.target === null ? '–' : `${fmtNumber(track.target, 1)} kW`],
                ['actual_kw', track.actual === null ? '–' : `${fmtNumber(track.actual, 1)} kW`],
                ...(money ? ([['revenue_usd', fmtNumber(money.revenue_usd, 4)]] as [string, string][]) : []),
              ]}
            />
          ) : null}
        </>
      ) : (
        <p className="corner-line muted">No power data yet.</p>
      )}
    </section>
  );
}

// ---- bottom left: controls -------------------------------------------------------------

function PresenterLink() {
  const presenter = useIsPresenter();
  const [open, setOpen] = useState(false);
  if (presenter) {
    return (
      <span className="corner-foot-note">
        Presenter mode on ·{' '}
        <button
          type="button"
          className="linklike"
          onClick={() => {
            leavePresenter();
            showToast({ kind: 'info', title: 'Presenter mode is off.' });
          }}
        >
          Leave
        </button>
      </span>
    );
  }
  return (
    <>
      <button type="button" className="linklike muted corner-foot-note" onClick={() => setOpen(true)}>
        Presenter
      </button>
      <PresenterDialog open={open} onClose={() => setOpen(false)} />
    </>
  );
}

export function ControlsCorner({ onOpenAvailability, onShowCharts }: { onOpenAvailability: () => void; onShowCharts: () => void }) {
  const presenter = useIsPresenter();
  return (
    <section className="corner corner-bl" aria-label="Simulation controls">
      <div className="corner-buttons">
        <PauseResume />
        <SpeedSelect />
        <ResetButton />
        <button type="button" className="btn" onClick={onOpenAvailability}>
          <Icon name="plug" />
          Take batteries offline
        </button>
        {presenter ? <CrashControl /> : null}
      </div>
      <div className="corner-foot">
        <button type="button" className="btn btn-quiet" onClick={onShowCharts}>
          <Icon name="chart" />
          Show charts
        </button>
        <PresenterLink />
      </div>
    </section>
  );
}

// ---- bottom right: controllers, batteries and the last events -----------------------------

const TILE: Record<WorkerStatus, { label: string; glyph: string }> = {
  healthy: { label: 'working', glyph: '' },
  suspect: { label: 'not responding', glyph: '!' },
  dead: { label: 'crashed', glyph: '✕' },
  starting: { label: 'starting', glyph: '…' },
};

function MiniTile({ worker, scope, offline }: { worker: Worker; scope: Scope; offline: boolean }) {
  const t = TILE[worker.status] ?? TILE.starting;
  const share = workerShare(worker, scope);
  const serving = share > 0;
  const where = scope === null ? '' : ` ${cityName(scope)}`;
  const text =
    worker.status === 'dead' && worker.batteries === 0
      ? `${controllerName(worker.worker_id)}: ${t.label}, its batteries were handed to the others`
      : `${controllerName(worker.worker_id)}: ${t.label}, runs ${fmtCount(share)}${where} ${plural(share, 'battery', 'batteries')}${offline ? ' (offline)' : ''}`;
  return (
    <li
      className={`mini-tile mini-tile-${worker.status}${scope !== null ? (serving ? ' is-serving' : ' is-idle') : ''}${offline ? ' is-offline' : ''}`}
      title={text}
    >
      <span aria-hidden="true">{t.glyph || worker.worker_id + 1}</span>
      <span className="sr-only">{text}</span>
    </li>
  );
}

const CODE_ROWS: { code: number; label: string; className: string }[] = [
  { code: SELLING, label: 'selling', className: 'code-selling' },
  { code: CHARGING, label: 'charging', className: 'code-charging' },
  { code: WAITING, label: 'waiting', className: 'code-waiting' },
  { code: OFFLINE, label: 'offline', className: 'code-offline' },
  { code: DOWN, label: 'waiting for a controller', className: 'code-down' },
];

function BatteryCounts({ scope }: { scope: Scope }) {
  const availability = useLive((s) => s.availability);
  const byCity = useMapStatus((s) => s.byCity);
  const fleet = useMapStatus((s) => s.fleet);
  const source = useMapStatus((s) => s.source);
  const statusChars = useMapStatus((s) => s.statusChars);
  // byCity is indexed like the layout's cities.
  const layoutCities = useLayoutState().layout?.cities ?? [];
  const index = scope === null ? -1 : layoutCities.findIndex((c) => c.location === scope);
  const counts = scope === null ? fleet : (byCity[index] ?? null);
  const a = scopeAvailability(availability, scope);
  return (
    <div className="corner-block">
      <h3 className="corner-label corner-label-row">
        <span>{scope === null ? 'Batteries' : `Batteries · ${cityName(scope)}`}</span>
        {a ? (
          <span className="corner-label-value">
            <span className="num">{fmtCount(a.available)}</span> of {fmtCount(a.total)} online
          </span>
        ) : null}
      </h3>
      {counts && counts.some((n) => n > 0) ? (
        <ul className="code-legend" aria-label="What the dots show">
          {CODE_ROWS.filter((r) => r.code !== DOWN || counts[DOWN] > 0).map((r) => (
            <li key={r.code} className={counts[r.code] ? '' : 'is-zero'}>
              <span className={`code-dot ${r.className}`} aria-hidden="true" />
              <span className="num">{fmtCount(counts[r.code])}</span> {r.label}
            </li>
          ))}
        </ul>
      ) : null}
      <TechOnly>
        <p className="tech">
          battery_status: {source === 'battery_status' ? `${fmtCount(statusChars)} base64 chars` : source === 'from_actions' ? 'missing, colours from each city action' : 'none yet'}
          {counts ? ` · codes 0-4 [${counts.join(', ')}]` : ''}
        </p>
      </TechOnly>
    </div>
  );
}

const SEVERITY_ICON: Record<string, IconName> = { good: 'check', warn: 'alert', info: 'info' };

function LastEvents() {
  const events = useLive((s) => s.events);
  const visible = events.slice(0, 3);
  return (
    <div className="corner-block">
      <h3 className="corner-label">What just happened</h3>
      {visible.length ? (
        <ol className="mini-feed" aria-live="polite" aria-relevant="additions">
          {visible.map((e) => {
            const when = e.sim_utc ?? e.wall_utc;
            return (
              <li key={e.id} className={`mini-feed-item feed-${e.severity in SEVERITY_ICON ? e.severity : 'info'}`} title={e.message}>
                <Icon name={SEVERITY_ICON[e.severity] ?? 'info'} size={14} />
                <span className="mini-feed-text">{e.message}</span>
                {when ? (
                  <time className="feed-time" dateTime={when}>
                    {fmtTimeOfDay(when)}
                  </time>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="corner-line muted">Notes appear here as the replay runs.</p>
      )}
    </div>
  );
}

export function FleetCorner({ scope }: { scope: Scope }) {
  const workers = useLive((s) => s.workers);
  const offlineWorkers = useLive((s) => s.availability?.offline_workers);
  const offline = new Set(offlineWorkers ?? []);
  const healthy = workers.filter((w) => w.status === 'healthy').length;
  const suspect = workers.filter((w) => w.status === 'suspect').length;
  const dead = workers.filter((w) => w.status === 'dead').length;
  const serving = scope === null ? 0 : workers.filter((w) => workerShare(w, scope) > 0).length;
  return (
    <section className="corner corner-br" aria-label={`Controllers and batteries, ${scopeLabel(scope)}`}>
      <h2 className="corner-label corner-label-row">
        <span>
          <Term plain="Controllers" tech="worker processes" />
        </span>
        {workers.length ? (
          <span className="corner-label-value">
            {healthy} of {workers.length} working
          </span>
        ) : null}
      </h2>
      {workers.length ? (
        <>
          {suspect || dead ? (
            <p className="corner-line">
              {suspect ? (
                <span className="chip chip-warn">
                  <Icon name="alert" size={13} />
                  {suspect} not responding
                </span>
              ) : null}
              {dead ? (
                <span className="chip chip-fail">
                  <Icon name="x" size={13} />
                  {dead} crashed
                </span>
              ) : null}
            </p>
          ) : null}
          <ul className="mini-tiles" aria-label="Controllers">
            {workers.map((w) => (
              <MiniTile key={w.worker_id} worker={w} scope={scope} offline={offline.has(w.worker_id)} />
            ))}
          </ul>
          {scope !== null ? (
            <p className="corner-sub">
              Highlighted: the {serving} {plural(serving, 'controller', 'controllers')} running {cityName(scope)} batteries.
            </p>
          ) : null}
        </>
      ) : (
        <p className="corner-line muted">The 20 controllers appear here when a simulation is running.</p>
      )}
      <hr className="corner-rule" />
      <BatteryCounts scope={scope} />
      <hr className="corner-rule" />
      <LastEvents />
    </section>
  );
}
