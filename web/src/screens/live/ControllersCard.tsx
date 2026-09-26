import { memo } from 'react';
import type { Worker, WorkerStatus } from '../../api/types.ts';
import { Icon, type IconName } from '../../components/Icon.tsx';
import { Panel } from '../../components/Panel.tsx';
import { TechOnly, Term } from '../../components/Tech.tsx';
import { useLive } from '../../live/liveStore.ts';
import { controllerFromDetail, controllerName } from '../../lib/fleet.ts';
import { fmtCount, fmtTimeOfDay, plural } from '../../lib/format.ts';
import { useTechnical } from '../../state/prefs.ts';
import { CrashPopover } from './controls.tsx';
import { useCrashAsk } from './crash.ts';

const STATUS: Record<WorkerStatus, { label: string; icon: IconName }> = {
  healthy: { label: 'Working', icon: 'check' },
  suspect: { label: 'Not responding', icon: 'alert' },
  dead: { label: 'Crashed', icon: 'x' },
  starting: { label: 'Starting', icon: 'clock' },
};

interface TileProps {
  worker: Worker;
  offline: boolean;
  technical: boolean;
  /** Why this controller cannot be crashed now, or null when a click can crash it. */
  blocked: string | null;
  asking: boolean;
  onAsk: (workerId: number, button: HTMLElement) => void;
}

/** Re-render a tile only when something it shows changed (every frame brings new worker objects). */
function sameTile(a: TileProps, b: TileProps): boolean {
  const x = a.worker;
  const y = b.worker;
  return (
    a.offline === b.offline &&
    a.technical === b.technical &&
    a.blocked === b.blocked &&
    a.asking === b.asking &&
    a.onAsk === b.onAsk &&
    x.worker_id === y.worker_id &&
    x.status === y.status &&
    x.batteries === y.batteries &&
    (!a.technical ||
      (x.pid === y.pid && x.last_tick_utc === y.last_tick_utc && x.adopted_from.join() === y.adopted_from.join()))
  );
}

/** A controller tile; clicking a working one asks "Crash controller 7?" (controls.tsx). */
const Tile = memo(function Tile({ worker, offline, technical, blocked, asking, onAsk }: TileProps) {
  const s = STATUS[worker.status] ?? STATUS.starting;
  const handedOff = worker.status === 'dead' && worker.batteries === 0;
  return (
    <li>
      <button
        type="button"
        className={`tile tile-${worker.status}${asking ? ' is-asking' : ''}`}
        disabled={blocked !== null}
        title={blocked ?? 'Click to crash this controller'}
        aria-haspopup="dialog"
        aria-expanded={asking}
        onClick={(e) => onAsk(worker.worker_id, e.currentTarget)}
      >
        <span className="tile-name">{controllerName(worker.worker_id)}</span>
        <span className="tile-status">
          <Icon name={s.icon} size={14} />
          {s.label}
        </span>
        <span className="tile-count">
          {handedOff ? 'Handed to others' : `${fmtCount(worker.batteries)} ${plural(worker.batteries, 'battery', 'batteries')}`}
        </span>
        {offline ? <span className="tile-tag">Its batteries are offline</span> : null}
        {technical ? (
          <span className="tech">
            worker_id {worker.worker_id} · pid {worker.pid ?? '–'}
            {worker.last_tick_utc ? ` · ${fmtTimeOfDay(worker.last_tick_utc)}` : ''}
            {worker.adopted_from?.length ? ` · adopted_from [${worker.adopted_from.join(', ')}]` : ''}
          </span>
        ) : null}
        {blocked === null ? <span className="sr-only">. Press to crash it.</span> : null}
      </button>
    </li>
  );
}, sameTile);

function Summary({ workers }: { workers: Worker[] }) {
  const availability = useLive((s) => s.availability);
  const count = (st: WorkerStatus) => workers.filter((w) => w.status === st).length;
  const healthy = count('healthy');
  const suspect = count('suspect');
  const dead = count('dead');
  const starting = count('starting');
  return (
    <div className="controller-summary" role="status">
      <strong>
        {healthy} of {workers.length} working
      </strong>
      {suspect ? (
        <span className="chip chip-warn">
          <Icon name="alert" size={14} />
          {suspect} not responding
        </span>
      ) : null}
      {dead ? (
        <span className="chip chip-fail">
          <Icon name="x" size={14} />
          {dead} crashed
        </span>
      ) : null}
      {starting ? <span className="muted">{starting} starting</span> : null}
      {availability ? (
        <span className="muted">
          Batteries online: {fmtCount(availability.available)} of {fmtCount(availability.total)}
        </span>
      ) : null}
    </div>
  );
}

/** The latest handover, narrated from the worker_adopted event's details (docs/API.md). */
function LatestHandover() {
  const event = useLive((s) => s.events.find((e) => e.kind === 'worker_adopted') ?? null);
  if (!event) return null;
  const controller = controllerFromDetail(event.detail);
  const adoptedBy = Array.isArray(event.detail?.adopted_by) ? event.detail.adopted_by.length : null;
  const ms = typeof event.detail?.ms === 'number' ? event.detail.ms : null;
  if (controller === null) return null;
  return (
    <p className="handover">
      <Icon name="check" size={14} />
      <span>
        Latest handover: Controller {controller}'s batteries went to
        {adoptedBy !== null ? ` the other ${adoptedBy} ${plural(adoptedBy, 'controller', 'controllers')}` : ' the others'}
        {ms !== null ? ` in ${fmtCount(ms)} ms` : ''}
        {event.sim_utc ? ` (at ${fmtTimeOfDay(event.sim_utc)} market time)` : ''}.
      </span>
    </p>
  );
}

function FailedCommandsCount() {
  const failed = useLive((s) => s.failedCommands);
  const technical = useTechnical();
  return (
    <div className="failed-commands">
      <span className="failed-count">
        <Term plain="Failed commands" tech="dead-letter entries" />: <span className="num">{fmtCount(failed.count)}</span>
      </span>
      <span className="muted">
        Orders that could not reach a battery after 3 tries. A burst is normal right after a controller crashes.
      </span>
      {technical && failed.recent.length ? (
        <div className="tech-block">
          <dl>
            {failed.recent.slice(0, 5).map((f, i) => (
              <div key={`${f.battery_id}-${f.tick_utc}-${i}`} style={{ display: 'contents' }}>
                <dt>{f.battery_id}</dt>
                <dd>
                  {controllerName(f.worker_id)} (worker_id {f.worker_id}) · {f.reason} · {fmtTimeOfDay(f.tick_utc)}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}
    </div>
  );
}

export function ControllersCard() {
  const workers = useLive((s) => s.workers);
  const offlineWorkers = useLive((s) => s.availability?.offline_workers);
  const status = useLive((s) => s.status);
  const technical = useTechnical();
  const crash = useCrashAsk();
  const offline = new Set(offlineWorkers ?? []);
  const healthy = workers.filter((w) => w.status === 'healthy').length;
  const running = status === 'running' || status === 'paused';
  const blockedFor = (w: Worker): string | null => {
    if (!running) return 'Start a simulation first';
    if (w.status !== 'healthy') return `${controllerName(w.worker_id)} is ${STATUS[w.status]?.label.toLowerCase() ?? 'not working'}`;
    if (healthy <= 2) return 'At least 2 controllers must keep working';
    return null;
  };
  return (
    <Panel
      title={<Term plain="Controllers" tech="worker processes" />}
      what="Each controller runs 1,000 batteries. If one crashes, the others take over its batteries. Click one to crash it."
      how={
        <p>
          Each controller is a separate program that sends orders to its batteries every 5 minutes of market time. If
          one stops answering it is marked "not responding" and the others cover for it; after two missed steps it is
          marked "crashed" and its batteries are handed out to the rest. Crashed controllers stay red until someone
          presses Reset.
        </p>
      }
    >
      {workers.length ? (
        <>
          <Summary workers={workers} />
          <div className="tiles-wrap">
            <ul className="tiles" aria-label="Controllers (click one to crash it)">
              {workers.map((w) => (
                <Tile
                  key={w.worker_id}
                  worker={w}
                  offline={offline.has(w.worker_id)}
                  technical={technical}
                  blocked={blockedFor(w)}
                  asking={crash.target?.workerId === w.worker_id}
                  onAsk={crash.ask}
                />
              ))}
            </ul>
            {crash.target ? <CrashPopover key={crash.target.workerId} target={crash.target} onClose={crash.close} /> : null}
          </div>
          <LatestHandover />
        </>
      ) : (
        <p className="feed-empty">The 20 controllers appear here when a simulation is running.</p>
      )}
      <FailedCommandsCount />
      <TechOnly>
        <p className="tech" style={{ marginTop: 6 }}>
          Controller N is worker_id N − 1; status comes from heartbeats and tick deadlines.
        </p>
      </TechOnly>
    </Panel>
  );
}
