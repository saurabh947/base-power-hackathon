// The live map's four corner panels (docs/UX.md, Live map > Corners, and "Live map v2"). Each one shows the
// selected city's numbers, or the whole fleet's for "All of Texas". The clock, money and fleet corners follow
// the timeline: they read useLiveData, which is the moment being viewed inside the map (src/live/timeTravel.ts).
// The controls always act on the live run.

import type { Tick, Worker, WorkerStatus } from '../../../api/types.ts';
import { AnimatedNumber } from '../../../components/AnimatedNumber.tsx';
import { Icon } from '../../../components/Icon.tsx';
import { TechBlock, TechOnly, Term } from '../../../components/Tech.tsx';
import { useLive } from '../../../live/liveStore.ts';
import { projectedMoney } from '../../../live/past.ts';
import { useLiveData, useViewing } from '../../../live/timeTravel.ts';
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
import { useTechnical } from '../../../state/prefs.ts';
import { ActionChip, ActionIcon } from '../ActionChip.tsx';
import { CrashButton, CrashPopover, PauseResume, ResetButton, SpeedSelect } from '../controls.tsx';
import { useCrashAsk } from '../crash.ts';
import { eventClass, eventIcon, eventSpoken } from '../eventLook.ts';
import { RaceBars } from '../RaceBars.tsx';
import { ConnectionStatus, PredictedBadge, RunStatus, TechCounters } from '../TopBar.tsx';
import { MiniPowerChart, MiniPriceChart } from './MiniCharts.tsx';
import { ON_TARGET_PCT, scopeAvailability, scopeTotals, scopeTracking, workerShare, type Scope } from './scope.ts';
import { Timeline } from './Timeline.tsx';

const lastTick = (s: { history: Tick[] }) => s.history[s.history.length - 1] ?? null;

/** "Houston" or "All of Texas". */
function scopeLabel(scope: Scope): string {
  return scope === null ? 'All of Texas' : cityName(scope);
}

/** "right now", or "at 7:45 pm" while the timeline shows an earlier moment. */
function useWhen(): string {
  const viewing = useViewing();
  return viewing === null ? 'right now' : `at ${fmtTimeOfDay(viewing)}`;
}

// ---- top left: clock, timeline and price ----------------------------------------------------

function MapClock() {
  const simTime = useLiveData((s) => s.simTimeUtc);
  const status = useLiveData((s) => s.status);
  const predicted = useLiveData((s) => lastTick(s)?.predicted === true);
  const viewing = useViewing() !== null;
  if (!simTime) return <p className="map-clock">{status === 'idle' ? 'No simulation running' : 'Replay clock'}</p>;
  const [when, zone] = fmtClock(simTime).split(/, (?=Central time$)/);
  return (
    <p className="map-clock">
      <span className="sr-only">{viewing ? 'Viewing an earlier moment: ' : 'Replay time: '}</span>
      <span className="nowrap">{when}</span>
      {zone ? (
        <>
          {' '}
          <span className="map-clock-zone nowrap">{zone}</span>
        </>
      ) : null}
      {predicted ? (
        <>
          {' '}
          <PredictedBadge />
        </>
      ) : null}
    </p>
  );
}

function CityPrice({ location }: { location: string }) {
  const tick = useLiveData(lastTick);
  const hasForecast = useLiveData((s) => !!s.forecast[location]);
  const anyPredicted = useLiveData((s) => s.history.some((t) => t.predicted));
  const when = useWhen();
  const price = tick?.price?.[location];
  const decision = tick?.decision_price?.[location];
  return (
    <div className="corner-block">
      <h3 className="corner-label">
        <Term plain={`Price ${when} · ${cityName(location)}`} tech="RT settlement point price" />
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
        {anyPredicted ? (
          <li>
            <span className="key-swatch key-predicted" aria-hidden="true" />
            AI prediction
          </li>
        ) : null}
        <li>
          <span className="key-swatch key-band" aria-hidden="true" />
          {hasForecast ? 'AI forecast, likely range' : 'AI forecast (none yet)'}
        </li>
      </ul>
      <TechOnly>
        <p className="tech">
          {location}
          {typeof decision === 'number' ? ` · acted on ${fmtPriceMwh(decision)} (15 min late)` : ''}
          {tick?.predicted ? ' · predicted: true' : ''}
        </p>
      </TechOnly>
    </div>
  );
}

function AllPrices({ onSelect }: { onSelect: (location: string) => void }) {
  const tick = useLiveData(lastTick);
  const cities = useCities();
  const when = useWhen();
  return (
    <div className="corner-block">
      <h3 className="corner-label">
        <Term plain={`Electricity price ${when}`} tech="RT settlement point price" />
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

export function ClockCorner({ scope, onSelect }: { scope: Scope; onSelect: (location: string) => void }) {
  return (
    <section className="corner corner-tl" aria-label="Replay clock, timeline and electricity price">
      <div className="corner-status">
        <RunStatus />
        <ConnectionStatus />
      </div>
      <MapClock />
      <Timeline scope={scope} />
      <TechCounters />
      <hr className="corner-rule" />
      {scope === null ? <AllPrices onSelect={onSelect} /> : <CityPrice location={scope} />}
    </section>
  );
}

// ---- top right: money, the race and power ------------------------------------------------------

const formatMoney = (n: number) => fmtUsd(n, 2);

/** Average battery power, for a city's "on target" floor (0.5% of its nameplate). */
function useAvgPowerKw(): number {
  return useLiveData((s) => {
    const r = s.settings?.power_kw_range;
    return r ? (r[0] + r[1]) / 2 : 8.25;
  });
}

/** "$6,790 of it is projected, on the AI's predicted prices" (docs/API.md Tick.predicted). */
function ProjectedLine({ scope }: { scope: Scope }) {
  const usd = useLiveData((s) => projectedMoney(s.history, scope)?.usd ?? null);
  const exact = useLiveData((s) => projectedMoney(s.history, scope)?.exact ?? true);
  if (usd === null) return null;
  return (
    <p className="corner-sub projected-line" title="Money earned while the prices are the AI's prediction, not real ERCOT data, is a projection.">
      <Icon name="ai" size={13} />
      {!exact ? (
        <span>Part of it is projected, on the AI’s predicted prices</span>
      ) : Math.abs(usd) >= 0.005 ? (
        <span>
          <strong className="num">{fmtUsd(usd, Math.abs(usd) < 100 ? 2 : 0)}</strong> of it is projected, on the AI’s
          predicted prices
        </span>
      ) : (
        <span>From here on it is projected: prices are the AI’s prediction</span>
      )}
    </p>
  );
}

export function MoneyCorner({ scope }: { scope: Scope }) {
  const totals = useLiveData((s) => s.totals);
  const received = useLiveData((s) => s.received);
  const tick = useLiveData(lastTick);
  const viewing = useViewing();
  const cities = useCities();
  const avgKw = useAvgPowerKw();
  const technical = useTechnical();
  const money = scopeTotals(totals, scope);
  const batteries = scope === null ? cities.reduce((a, c) => a + c.batteries, 0) : (cities.find((c) => c.location === scope)?.batteries ?? 0);
  const track = scopeTracking(tick, scope, batteries, avgKw);
  const onTarget = track.errorPct !== null ? track.errorPct <= ON_TARGET_PCT : null;
  const gap = track.target !== null && track.actual !== null ? Math.abs(track.target - track.actual) : null;
  const where = scope === null ? 'in this replay' : `in ${cityName(scope)}`;
  const by = viewing === null ? 'so far' : `by ${fmtTimeOfDay(viewing)}`;

  return (
    <section className="corner corner-tr" aria-label={`Money and power, ${scopeLabel(scope)}`}>
      <h2 className="corner-label">
        Earned {by} {where}
      </h2>
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
      <ProjectedLine scope={scope} />
      <RaceBars scope={scope} />
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

// ---- bottom left: controls (always the live run) ---------------------------------------------

export function ControlsCorner({ onOpenAvailability, onShowCharts }: { onOpenAvailability: () => void; onShowCharts: () => void }) {
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
        <span className="crash-with-hint">
          <CrashButton />
          <span className="corner-hint">or click a controller</span>
        </span>
      </div>
      <div className="corner-foot">
        <button type="button" className="btn btn-quiet" onClick={onShowCharts}>
          <Icon name="chart" />
          Show charts
        </button>
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

interface TileProps {
  worker: Worker;
  scope: Scope;
  offline: boolean;
  /** Why this tile cannot be crashed now, or null when it can. */
  blocked: string | null;
  asking: boolean;
  onAsk: (workerId: number, button: HTMLElement) => void;
}

function MiniTile({ worker, scope, offline, blocked, asking, onAsk }: TileProps) {
  const t = TILE[worker.status] ?? TILE.starting;
  const share = workerShare(worker, scope);
  const serving = share > 0;
  const where = scope === null ? '' : ` ${cityName(scope)}`;
  const text =
    worker.status === 'dead' && worker.batteries === 0
      ? `${controllerName(worker.worker_id)}: ${t.label}, its batteries were handed to the others`
      : `${controllerName(worker.worker_id)}: ${t.label}, runs ${fmtCount(share)}${where} ${plural(share, 'battery', 'batteries')}${offline ? ' (offline)' : ''}`;
  const hint = blocked ?? 'Click to crash it';
  return (
    <li>
      <button
        type="button"
        className={`mini-tile mini-tile-${worker.status}${scope !== null ? (serving ? ' is-serving' : ' is-idle') : ''}${offline ? ' is-offline' : ''}${asking ? ' is-asking' : ''}`}
        title={`${text}. ${hint}.`}
        aria-label={`${text}.${blocked ? '' : ' Press to crash it.'}`}
        aria-haspopup="dialog"
        aria-expanded={asking}
        disabled={blocked !== null}
        onClick={(e) => onAsk(worker.worker_id, e.currentTarget)}
      >
        <span aria-hidden="true">{t.glyph || worker.worker_id + 1}</span>
      </button>
    </li>
  );
}

/** Legend rows: the same looks as on the map (src/map/looks.ts), doubling as the battery counts. */
const CODE_ROWS: { code: number; label: string; className: string }[] = [
  { code: SELLING, label: 'selling', className: 'code-selling' },
  { code: CHARGING, label: 'charging', className: 'code-charging' },
  { code: WAITING, label: 'waiting', className: 'code-waiting' },
  { code: OFFLINE, label: 'offline', className: 'code-offline' },
  { code: DOWN, label: 'controller crashed', className: 'code-down' },
];

function BatteryCounts({ scope }: { scope: Scope }) {
  const availability = useLiveData((s) => s.availability);
  const byCity = useMapStatus((s) => s.byCity);
  const fleet = useMapStatus((s) => s.fleet);
  const source = useMapStatus((s) => s.source);
  const statusChars = useMapStatus((s) => s.statusChars);
  const fromEvents = useMapStatus((s) => s.fromEvents);
  // byCity is indexed like the layout's cities.
  const layoutCities = useLayoutState().layout?.cities ?? [];
  const index = scope === null ? -1 : layoutCities.findIndex((c) => c.location === scope);
  const counts = scope === null ? fleet : (byCity[index] ?? null);
  const a = scopeAvailability(availability, scope);
  // Before this page was opened only each city's overall action is known: no per-battery counts.
  const cityLevel = source === 'city_level';
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
          {CODE_ROWS.map((r) => (
            <li key={r.code} className={counts[r.code] || cityLevel ? '' : 'is-zero'}>
              <span className={`code-dot ${r.className}`} aria-hidden="true" />
              {cityLevel ? null : <span className="num">{fmtCount(counts[r.code])}</span>} {r.label}
            </li>
          ))}
          <li className="code-edge-row">
            <span className="code-dot code-edge" aria-hidden="true" />
            red edge: some offline or crashed
          </li>
        </ul>
      ) : null}
      {cityLevel ? <p className="corner-sub">Before you opened this page, each city’s dots show what it did as a whole.</p> : null}
      <TechOnly>
        <p className="tech">
          battery_status:{' '}
          {source === 'battery_status'
            ? `${fmtCount(statusChars)} base64 chars`
            : source === 'recorded'
              ? `recorded by this page (${fmtCount(statusChars)} bytes)`
              : source === 'city_level'
                ? 'not recorded (before this page opened): each city action'
                : source === 'from_actions'
                  ? 'missing, colours from each city action'
                  : 'none yet'}
          {counts ? ` · codes 0-4 [${counts.join(', ')}]` : ''}
          {fromEvents ? ` · ${fmtCount(fromEvents)} shown down from the controller events (the frame missed code 4)` : ''}
        </p>
      </TechOnly>
    </div>
  );
}

function LastEvents() {
  const events = useLiveData((s) => s.events);
  const visible = events.slice(0, 3);
  return (
    <div className="corner-block">
      <h3 className="corner-label">What just happened</h3>
      {visible.length ? (
        <ol className="mini-feed" aria-live="polite" aria-relevant="additions">
          {visible.map((e) => {
            const when = e.sim_utc ?? e.wall_utc;
            return (
              <li key={e.id} className={`mini-feed-item ${eventClass(e)}`} title={e.message}>
                <Icon name={eventIcon(e)} size={14} />
                <span className="mini-feed-text">
                  <span className="sr-only">{eventSpoken(e)}</span>
                  {e.message}
                </span>
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
  const workers = useLiveData((s) => s.workers);
  const offlineWorkers = useLiveData((s) => s.availability?.offline_workers);
  const runStatus = useLive((s) => s.status);
  const viewing = useViewing();
  const crash = useCrashAsk();
  const offline = new Set(offlineWorkers ?? []);
  const healthy = workers.filter((w) => w.status === 'healthy').length;
  const suspect = workers.filter((w) => w.status === 'suspect').length;
  const dead = workers.filter((w) => w.status === 'dead').length;
  const serving = scope === null ? 0 : workers.filter((w) => workerShare(w, scope) > 0).length;
  const running = runStatus === 'running' || runStatus === 'paused';
  const blockedFor = (w: Worker): string | null => {
    if (viewing !== null) return 'Go back to live to crash a controller';
    if (!running) return 'Start a simulation first';
    if (w.status !== 'healthy') return `It is ${TILE[w.status]?.label ?? 'not working'}`;
    if (healthy <= 2) return 'At least 2 controllers must keep working';
    return null;
  };
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
          <div className="mini-tiles-wrap">
            <ul className="mini-tiles" aria-label="Controllers (click one to crash it)">
              {workers.map((w) => (
                <MiniTile
                  key={w.worker_id}
                  worker={w}
                  scope={scope}
                  offline={offline.has(w.worker_id)}
                  blocked={blockedFor(w)}
                  asking={crash.target?.workerId === w.worker_id}
                  onAsk={crash.ask}
                />
              ))}
            </ul>
            {crash.target ? <CrashPopover key={crash.target.workerId} target={crash.target} onClose={crash.close} /> : null}
          </div>
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
