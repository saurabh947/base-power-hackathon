import { memo } from 'react';
import type { Worker, WorkerStatus } from '../../api/types.ts';
import { Icon, type IconName } from '../../components/Icon.tsx';
import { Panel } from '../../components/Panel.tsx';
import { TechOnly, Term } from '../../components/Tech.tsx';
import { useLive } from '../../live/liveStore.ts';
import { fmtCount, fmtTimeOfDay, plural } from '../../lib/format.ts';
import { useTechnical } from '../../state/prefs.ts';

const STATUS: Record<WorkerStatus, { label: string; icon: IconName }> = {
  healthy: { label: 'Working', icon: 'check' },
  suspect: { label: 'Not responding', icon: 'alert' },
  dead: { label: 'Crashed', icon: 'x' },
  starting: { label: 'Starting', icon: 'clock' },
};

const Tile = memo(function Tile({ worker, offline, technical }: { worker: Worker; offline: boolean; technical: boolean }) {
  const s = STATUS[worker.status] ?? STATUS.starting;
  const handedOff = worker.status === 'dead' && worker.batteries === 0;
  return (
    <li className={`tile tile-${worker.status}`}>
      <span className="tile-name">Controller {worker.worker_id}</span>
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
          pid {worker.pid ?? '–'}
          {worker.last_tick_utc ? ` · ${fmtTimeOfDay(worker.last_tick_utc)}` : ''}
          {worker.adopted_from?.length ? ` · adopted ${worker.adopted_from.join(', ')}` : ''}
        </span>
      ) : null}
    </li>
  );
});

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
                  worker {f.worker_id} · {f.reason} · {fmtTimeOfDay(f.tick_utc)}
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
  const technical = useTechnical();
  const offline = new Set(offlineWorkers ?? []);
  return (
    <Panel
      title={<Term plain="Controllers" tech="worker processes" />}
      what="Each controller runs 1,000 batteries. If one crashes, the others take over its batteries."
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
          <ul className="tiles" aria-label="Controllers">
            {workers.map((w) => (
              <Tile key={w.worker_id} worker={w} offline={offline.has(w.worker_id)} technical={technical} />
            ))}
          </ul>
        </>
      ) : (
        <p className="feed-empty">The 20 controllers appear here when a simulation is running.</p>
      )}
      <FailedCommandsCount />
      <TechOnly>
        <p className="tech" style={{ marginTop: 6 }}>
          Tiles are keyed by worker_id; status comes from heartbeats and tick deadlines.
        </p>
      </TechOnly>
    </Panel>
  );
}
