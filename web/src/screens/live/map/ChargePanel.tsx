// "Charge by controller", the map's centre-right panel: the 20 controllers as battery gauges, each filled to
// the usable charge of the batteries it runs, with its status: green when working, an amber outline when not
// responding, red with ✕ once crashed (0%, until someone restarts it), a dashed outline and a ghost mark when its
// batteries were taken offline (half their charge gone). Selecting a city highlights the controllers serving
// it. Clicking a gauge asks "Crash controller 7?", or "Restart controller 7?" for a crashed one. It follows the
// timeline: the charge comes from the frames this page recorded; before the page was opened it is unknown and
// the gauges are dimmed. The model and the words are in ./gauges.ts.

import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Icon } from '../../../components/Icon.tsx';
import { TechOnly, Term } from '../../../components/Tech.tsx';
import { useLive } from '../../../live/liveStore.ts';
import { useLiveData, useViewing } from '../../../live/timeTravel.ts';
import { cityName } from '../../../lib/cities.ts';
import { fmtCount, fmtNumber } from '../../../lib/format.ts';
import { useTechnical } from '../../../state/prefs.ts';
import { ControllerPopover } from '../controls.tsx';
import { useControllerAsk } from '../crash.ts';
import {
  buildGauges,
  gaugeAction,
  gaugeChanges,
  gaugeWords,
  summarize,
  type Gauge,
  type GaugeAction,
  type GaugeChange,
  type GaugeSummary,
} from './gauges.ts';
import type { Scope } from './scope.ts';

const NONE: number[] = [];

/** How long a sudden drop or refill stays highlighted. */
const CHANGE_MS = 1400;
/** How far back "where it stood lately" looks, for a crash reported a frame or two after it emptied the batteries. */
const PEAK_MS = 4000;

interface Change extends GaugeChange {
  until: number;
}

/**
 * Sudden drops (a crash, going offline) and refills (a restart) between live frames of the same run, each kept
 * for CHANGE_MS so the gauge can show it. Moving along the timeline never animates.
 */
function useGaugeChanges(gauges: Gauge[], runId: string | null, past: boolean): Map<number, Change> {
  const prev = useRef<{ runId: string | null; past: boolean; gauges: Gauge[] } | null>(null);
  // Each gauge's recent levels, to know where it stood before a crash emptied it.
  const recent = useRef(new Map<number, { t: number; fill: number }[]>());
  const [active, setActive] = useState<Map<number, Change>>(() => new Map());

  useEffect(() => {
    const before = prev.current;
    prev.current = { runId, past, gauges };
    const now = Date.now();
    if (!before || past || before.past || before.runId !== runId) {
      recent.current.clear();
      return;
    }
    const peak = new Map<number, number>();
    for (const [id, list] of recent.current) peak.set(id, Math.max(...list.map((x) => x.fill)));
    const changes = gaugeChanges(before.gauges, gauges, peak);
    for (const g of gauges) {
      const list = (recent.current.get(g.workerId) ?? []).filter((x) => now - x.t < PEAK_MS);
      if (g.fill !== null) list.push({ t: now, fill: g.fill });
      recent.current.set(g.workerId, list);
    }
    if (!changes.size) return;
    const until = now + CHANGE_MS;
    setActive((m) => {
      const next = new Map(m);
      for (const [id, change] of changes) next.set(id, { ...change, until });
      return next;
    });
  }, [gauges, runId, past]);

  useEffect(() => {
    if (!active.size) return;
    const soonest = Math.min(...[...active.values()].map((c) => c.until));
    const timer = setTimeout(
      () => setActive((m) => new Map([...m].filter(([, c]) => c.until > Date.now()))),
      Math.max(0, soonest - Date.now()) + 20,
    );
    return () => clearTimeout(timer);
  }, [active]);

  return past ? new Map() : active;
}

interface GaugeProps {
  gauge: Gauge;
  scope: Scope;
  action: GaugeAction;
  asking: boolean;
  change: Change | undefined;
  technical: boolean;
  raw: { usable: number | null | undefined; level: number | null | undefined; pid: number | null };
  onAsk: (workerId: number, button: HTMLElement) => void;
}

function GaugeButton({ gauge: g, scope, action, asking, change, technical, raw, onAsk }: GaugeProps) {
  const words = gaugeWords(g, scope);
  const style = { '--fill': g.fill ?? 0, '--ghost': g.ghost ?? 0, '--was': change?.was ?? 0 } as CSSProperties;
  const classes = [
    'gauge',
    `is-${g.look}`,
    g.fill === null ? 'is-unknown' : '',
    g.offline ? 'is-offline' : '',
    g.serving === true ? 'is-serving' : g.serving === false ? 'is-idle' : '',
    action.action ? `can-${action.action}` : '',
    asking ? 'is-asking' : '',
    change ? `is-${change.kind}` : '',
  ]
    .filter(Boolean)
    .join(' ');
  return (
    <li>
      <button
        type="button"
        className={classes}
        style={style}
        aria-label={`${words.title}. ${words.detail} ${action.hint}.`}
        aria-haspopup={action.action ? 'dialog' : undefined}
        aria-expanded={action.action ? asking : undefined}
        aria-disabled={action.action ? undefined : true}
        onClick={(e) => {
          if (action.action) onAsk(g.workerId, e.currentTarget);
        }}
      >
        <span className="gauge-cell" aria-hidden="true">
          {g.ghost !== null ? <span className="gauge-ghost" /> : null}
          {change?.kind === 'drop' ? <span key={change.until} className="gauge-was" /> : null}
          <span className="gauge-fill" />
          <span className="gauge-num">{g.look === 'crashed' ? '✕' : g.number}</span>
        </span>
        <span className="gauge-tip" aria-hidden="true">
          <strong>{words.title}</strong>
          <span>{words.detail}</span>
          <em>{action.hint}</em>
          {technical ? (
            <span className="tech">
              usable_charge {raw.usable === undefined ? 'missing' : raw.usable === null ? 'null' : fmtNumber(raw.usable, 4)} · charge_level{' '}
              {raw.level === undefined ? 'missing' : raw.level === null ? 'null' : fmtNumber(raw.level, 4)} · pid {raw.pid ?? '–'}
            </span>
          ) : null}
        </span>
      </button>
    </li>
  );
}

/** One line under the title, only when there is something to say: who is down, who serves the city, or why the charge is unknown. */
function StatusLine({ summary, scope, past }: { summary: GaugeSummary; scope: Scope; past: boolean }) {
  if (!summary.total) return null;
  const unknown = !summary.known;
  const serving = scope !== null && summary.serving !== null;
  if (!unknown && !summary.suspect && !summary.crashed && !summary.offline && !serving) return null;
  return (
    <p className="corner-line charge-line">
      {unknown ? (
        <span className="charge-hint">
          {past ? 'Charge not recorded before you opened this page.' : 'This simulator does not report charge per controller.'}
        </span>
      ) : null}
      {summary.suspect ? (
        <span className="chip chip-warn">
          <Icon name="alert" size={13} />
          {summary.suspect} not responding
        </span>
      ) : null}
      {summary.crashed ? (
        <span className="chip chip-fail">
          <Icon name="x" size={13} />
          {summary.crashed} crashed
        </span>
      ) : null}
      {summary.offline ? (
        <span className="chip chip-offline">
          <Icon name="plug" size={13} />
          {summary.offline} offline
        </span>
      ) : null}
      {serving ? (
        <span className="charge-serving">
          <span className="sr-only">Highlighted: </span>
          {summary.serving} serving {cityName(scope)}
        </span>
      ) : null}
    </p>
  );
}

const EXPLAIN =
  'Each gauge is one controller: how much usable charge its batteries have left above the 20% reserve. A crash empties them (0%) until someone restarts it; taking its batteries offline loses half their charge.';

export function ChargePanel({ scope }: { scope: Scope }) {
  const workers = useLiveData((s) => s.workers);
  const offlineWorkers = useLiveData((s) => s.availability?.offline_workers ?? NONE);
  const runId = useLiveData((s) => s.runId);
  const liveStatus = useLive((s) => s.status);
  const failed = useLive((s) => s.failedCommands.count);
  const viewing = useViewing();
  const technical = useTechnical();
  const ask = useControllerAsk();
  const past = viewing !== null;

  const gauges = useMemo(() => buildGauges(workers, scope, offlineWorkers), [workers, scope, offlineWorkers]);
  const summary = summarize(gauges);
  const changes = useGaugeChanges(gauges, runId, past);
  const running = liveStatus === 'running' || liveStatus === 'paused';
  const ctx = { past, running, working: summary.working };
  const raw = new Map(workers.map((w) => [w.worker_id, { usable: w.usable_charge, level: w.charge_level, pid: w.pid }]));

  return (
    <section
      className={`corner corner-mr charge${summary.known ? '' : ' is-unknown'}`}
      aria-label={`Charge by controller, ${scope === null ? 'All of Texas' : cityName(scope)}`}
    >
      <h2 className="corner-label corner-label-row">
        <span title={EXPLAIN}>
          <Term plain="Charge by controller" tech="usable_charge per worker" />
        </span>
        {gauges.length ? (
          <span className="corner-label-value">
            {summary.working} of {summary.total} working
          </span>
        ) : null}
      </h2>
      <p className="sr-only">{EXPLAIN}</p>
      <StatusLine summary={summary} scope={scope} past={past} />
      {gauges.length ? (
        <div className="gauges-wrap">
          <ul className="gauges" aria-label="Controllers (click a working one to crash it, a crashed one to restart it)">
            {gauges.map((g) => (
              <GaugeButton
                key={g.workerId}
                gauge={g}
                scope={scope}
                action={gaugeAction(g, ctx)}
                asking={ask.target?.workerId === g.workerId}
                change={changes.get(g.workerId)}
                technical={technical}
                raw={raw.get(g.workerId) ?? { usable: undefined, level: undefined, pid: null }}
                onAsk={ask.ask}
              />
            ))}
          </ul>
          {ask.target ? <ControllerPopover key={ask.target.workerId} target={ask.target} onClose={ask.close} /> : null}
        </div>
      ) : (
        <p className="corner-line muted">The 20 controllers appear here when a simulation is running.</p>
      )}
      <TechOnly>
        <p className="tech">
          Controller N is worker_id N − 1 · usable_charge = (charge_level − 0.2) / 0.8 · failed commands (dead-letter){' '}
          {fmtCount(failed)}
        </p>
      </TechOnly>
    </section>
  );
}
