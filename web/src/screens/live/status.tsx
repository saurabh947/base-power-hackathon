// The run's and the connection's status lines, and the "Predicted" badge, for the map's top-left corner.

import { Icon } from '../../components/Icon.tsx';
import { TechOnly } from '../../components/Tech.tsx';
import { reconnectNow, useConnection, useLive } from '../../live/liveStore.ts';

/** Next to the clock on predicted ticks (docs/API.md Tick.predicted): these prices are the AI's, not ERCOT's. */
export function PredictedBadge() {
  return (
    <span
      className="chip chip-predicted"
      title="These prices are the AI's prediction (TimesFM-3), not real ERCOT data. Money earned on them is projected."
    >
      <Icon name="ai" size={13} />
      Predicted
    </span>
  );
}

export function RunStatus() {
  const status = useLive((s) => s.status);
  const received = useLive((s) => s.received);
  if (!received || !status) return null;
  const map = {
    running: ['dot-ok dot-pulse', 'Running'],
    paused: ['dot-warn', 'Paused'],
    starting: ['dot-warn dot-pulse', 'Starting…'],
    finished: ['', 'Finished'],
    idle: ['', 'Not running'],
  } as const;
  const [dot, label] = map[status];
  return (
    <span className="status-line">
      <span className={`dot ${dot}`} aria-hidden="true" />
      {label}
    </span>
  );
}

export function ConnectionStatus() {
  const mode = useConnection((s) => s.mode);
  const lastError = useConnection((s) => s.lastError);
  let content: [string, string] | null = null;
  if (mode === 'live') content = ['dot-ok', 'Live updates'];
  else if (mode === 'connecting') content = ['dot-warn dot-pulse', 'Connecting…'];
  else if (mode === 'reconnecting') content = ['dot-warn dot-pulse', 'Reconnecting…'];
  else if (mode === 'polling') content = ['dot-warn', 'Updating every 2 s (live connection unavailable)'];
  else if (mode === 'offline') content = ['dot-fail', "Can't reach the simulator. Retrying…"];
  if (!content) return null;
  return (
    <span className="status-line" role="status">
      <span className={`dot ${content[0]}`} aria-hidden="true" />
      {content[1]}
      {mode === 'offline' || mode === 'reconnecting' ? (
        <button type="button" className="linklike" onClick={reconnectNow}>
          Try now
        </button>
      ) : null}
      <TechOnly>{lastError ? <span className="tech">({lastError})</span> : null}</TechOnly>
    </span>
  );
}

export function TechCounters() {
  const messages = useLive((s) => s.messages);
  const ticks = useLive((s) => s.totals.ticks);
  const transport = useConnection((s) => s.transport);
  return (
    <TechOnly>
      <span className="tech">
        {transport ?? 'no transport'} · {messages} messages · tick {ticks}
      </span>
    </TechOnly>
  );
}
