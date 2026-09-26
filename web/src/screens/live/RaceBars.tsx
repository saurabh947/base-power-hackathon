// The race, drawn (docs/API.md Race): two labelled bars, the live strategy's in green, and who is ahead.
// On the map it sits in the money corner, for all of Texas or the selected city; "Show charts" has it
// in its money card. Hidden while idle (race is null). The numbers and words come from ./race.ts.

import { Icon } from '../../components/Icon.tsx';
import { useLive } from '../../live/liveStore.ts';
import { cityName } from '../../lib/cities.ts';
import { fmtNumber } from '../../lib/format.ts';
import type { LiveState } from '../../live/reducer.ts';
import { useTechnical } from '../../state/prefs.ts';
import { ActionIcon } from './ActionChip.tsx';
import type { Scope } from './map/scope.ts';
import { raceName, raceView } from './race.ts';

const lastTick = (s: LiveState) => s.history[s.history.length - 1] ?? null;

const width = (share: number) => `${Math.round(Math.min(1, Math.max(0, share)) * 1000) / 10}%`;

interface Props {
  scope: Scope;
  /** "corner": compact, for the map; "card": the "Show charts" money card, with its explainer showing. */
  variant?: 'corner' | 'card';
}

export function RaceBars({ scope, variant = 'corner' }: Props) {
  const race = useLive((s) => s.race);
  const totals = useLive((s) => s.totals);
  const tick = useLive(lastTick);
  const liveStrategy = useLive((s) => s.settings?.strategy ?? null);
  const technical = useTechnical();
  const view = raceView({ race, liveStrategy, totals, tick, scope });
  if (!race || !view) return null;

  const corner = variant === 'corner';
  const title = `Race against ${raceName(race)}`;
  const what = `A copy of the same batteries runs ${raceName(race)} on the same prices, side by side.`;
  const lead = (
    <span className={`race-lead is-${view.leader}`}>
      {view.leader === 'live' ? <Icon name="up" size={13} /> : null}
      {view.lead}
    </span>
  );
  return (
    <section
      className={`race race-${variant}`}
      aria-label={`${title}${scope === null ? '' : `, ${cityName(scope)}`}`}
      // The corner has little room at 1280 × 800: its explainer and the charge levels are in the tooltip.
      title={corner ? [what, view.charge].filter(Boolean).join('\n') : undefined}
    >
      {corner ? (
        // "RACE  ↑ AI forecast ahead by $15,290" on one line; the money above already names the city.
        <div className="race-head">
          <h3 className="corner-label">
            Race<span className="sr-only"> against {raceName(race)}</span>
          </h3>
          {lead}
        </div>
      ) : (
        <>
          <h3 className="race-title">{title}</h3>
          <p className="race-what">{what}</p>
        </>
      )}
      {corner ? <p className="sr-only">{what}</p> : null}
      <ul className={`race-rows${scope === null ? '' : ' with-actions'}`}>
        {[view.live, view.race].map((side, i) => (
          <li key={i === 0 ? 'live' : 'race'} className={`race-row${i === 0 ? ' is-live' : ''}`}>
            <span className="race-name">{side.label}</span>
            <span className="race-track" aria-hidden="true">
              <span className="race-fill" style={{ width: width(side.share) }} />
            </span>
            <span className="race-value num">{side.text}</span>
            {scope === null ? null : <ActionIcon action={side.action} />}
          </li>
        ))}
      </ul>
      {corner ? (
        view.charge ? <p className="sr-only">{view.charge}</p> : null
      ) : (
        <p className="race-foot">
          {lead}
          {view.charge ? <span className="race-charge">{view.charge}</span> : null}
        </p>
      )}
      {technical ? (
        <p className="tech">
          race: {race.strategy} · live_minus_race_usd {fmtNumber(race.live_minus_race_usd, 2)} · charge_level{' '}
          {fmtNumber(race.charge_level, 3)}
          {scope === null ? '' : ` · by_location.${scope} ${JSON.stringify(race.by_location?.[scope] ?? null)}`}
        </p>
      ) : null}
    </section>
  );
}
