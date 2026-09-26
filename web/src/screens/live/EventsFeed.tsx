import type { LiveEvent } from '../../api/types.ts';
import { Icon } from '../../components/Icon.tsx';
import { Panel } from '../../components/Panel.tsx';
import { useLive } from '../../live/liveStore.ts';
import { fmtTimeOfDay } from '../../lib/format.ts';
import { useTechnical } from '../../state/prefs.ts';
import { eventClass, eventIcon, eventSpoken } from './eventLook.ts';

const MAX_VISIBLE = 6;

function Item({ event, technical }: { event: LiveEvent; technical: boolean }) {
  const when = event.sim_utc ?? event.wall_utc;
  return (
    <li className={`feed-item ${eventClass(event)}`}>
      <Icon name={eventIcon(event)} size={16} />
      <span>
        <span className="sr-only">{eventSpoken(event)}</span>
        {event.message}
        {technical ? (
          <span className="tech">
            {' '}
            [{event.kind}
            {event.detail && Object.keys(event.detail).length ? ` ${JSON.stringify(event.detail)}` : ''}]
          </span>
        ) : null}
      </span>
      {when ? (
        <time className="feed-time" dateTime={when}>
          {fmtTimeOfDay(when)}
        </time>
      ) : (
        <span />
      )}
    </li>
  );
}

export function EventsFeed() {
  const events = useLive((s) => s.events);
  const technical = useTechnical();
  const visible = events.slice(0, MAX_VISIBLE);
  return (
    <Panel title="What just happened" what="Plain-language notes from the simulator, newest first. Times are market time.">
      {visible.length ? (
        <ol className="feed" aria-live="polite" aria-relevant="additions">
          {visible.map((e) => (
            <Item key={e.id} event={e} technical={technical} />
          ))}
        </ol>
      ) : (
        <p className="feed-empty">Nothing yet. Notes appear here as the replay runs.</p>
      )}
    </Panel>
  );
}
