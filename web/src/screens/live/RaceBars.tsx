// The race, drawn (docs/API.md Race): labelled bars, the live strategy's in green, and who is ahead. Once
// batteries went offline or a controller crashed (race.shows_potential), a third, hatched bar shows what the
// live strategy would have earned with every battery online (a projection), with what the outages cost.
// It sits in the map's money corner, compact, for all of Texas or the selected city, and follows the
// timeline. Hidden while idle (race is null). The words come from ./race.ts.

import { Icon } from '../../components/Icon.tsx';
import { useLiveData } from '../../live/timeTravel.ts';
import { cityName } from '../../lib/cities.ts';
import { fmtNumber } from '../../lib/format.ts';
import type { LiveState } from '../../live/reducer.ts';
import { useTechnical } from '../../state/prefs.ts';
import { ActionIcon } from './ActionChip.tsx';
import type { Scope } from './map/scope.ts';
import { POTENTIAL_LABEL, raceName, raceView, type RaceSide } from './race.ts';

const lastTick = (s: LiveState) => s.history[s.history.length - 1] ?? null;

const width = (share: number) => `${Math.round(Math.min(1, Math.max(0, share)) * 1000) / 10}%`;

function Row({ side, kind, scope }: { side: RaceSide; kind: 'live' | 'race' | 'potential'; scope: Scope }) {
  // The corner is narrow: "All online" (its note line says "projected"); the full label is in the tooltip.
  const name = kind === 'potential' ? 'All online' : side.label;
  return (
    <li className={`race-row is-${kind}`} title={kind === 'potential' ? `${POTENTIAL_LABEL} (projected)` : undefined}>
      <span className="race-name">{name}</span>
      <span className="race-track" aria-hidden="true">
        <span className="race-fill" style={{ width: width(side.share) }} />
      </span>
      <span className="race-value num">{side.text}</span>
      {scope === null ? null : kind === 'potential' ? <span aria-hidden="true" /> : <ActionIcon action={side.action} />}
    </li>
  );
}

export function RaceBars({ scope }: { scope: Scope }) {
  const race = useLiveData((s) => s.race);
  const totals = useLiveData((s) => s.totals);
  const tick = useLiveData(lastTick);
  const projected = useLiveData((s) => s.history.some((t) => t.predicted));
  const liveStrategy = useLiveData((s) => s.settings?.strategy ?? null);
  const technical = useTechnical();
  const view = raceView({ race, liveStrategy, totals, tick, scope });
  if (!race || !view) return null;

  const title = `Race against ${raceName(race)}`;
  const what = `A copy of the same batteries runs ${raceName(race)} on the same prices, side by side.`;
  const potentialWhat = view.potential
    ? `${POTENTIAL_LABEL} (projected): what the same strategy would have earned with every battery online, no controller crashes and no charge lost to them.`
    : null;
  const projectedNote = projected ? 'Sums include money projected on the AI’s predicted prices.' : null;
  return (
    <section
      className="race"
      aria-label={`${title}${scope === null ? '' : `, ${cityName(scope)}`}`}
      // The corner has little room at 1280 × 800: its explainers and the charge levels are in the tooltip.
      title={[what, potentialWhat, view.charge, projectedNote].filter(Boolean).join('\n')}
    >
      {/* "RACE  ↑ AI forecast ahead by $15,290" on one line; the money above already names the city. */}
      <div className="race-head">
        <h3 className="corner-label">
          Race<span className="sr-only"> against {raceName(race)}</span>
        </h3>
        <span className={`race-lead is-${view.leader}`}>
          {view.leader === 'live' ? <Icon name="up" size={13} /> : null}
          {view.lead}
        </span>
      </div>
      <p className="sr-only">{[what, potentialWhat, projectedNote].filter(Boolean).join(' ')}</p>
      <ul className={`race-rows${scope === null ? '' : ' with-actions'}`}>
        <Row side={view.live} kind="live" scope={scope} />
        <Row side={view.race} kind="race" scope={scope} />
        {view.potential ? <Row side={view.potential} kind="potential" scope={scope} /> : null}
        {/* One line under the third bar: "projected · Outages cost $790 so far". */}
        {view.potential ? (
          <li className="race-note">
            <span className="race-note-projected">projected</span>
            {view.shortfall ? (
              <>
                <span aria-hidden="true"> · </span>
                <span className={`race-shortfall-text${view.shortfall.usd >= 0.005 ? ' is-cost' : ''}`}>{view.shortfall.text}</span>
              </>
            ) : null}
          </li>
        ) : null}
      </ul>
      {view.charge ? <p className="sr-only">{view.charge}</p> : null}
      {technical ? (
        <p className="tech">
          race: {race.strategy} · live_minus_race_usd {fmtNumber(race.live_minus_race_usd, 2)} · charge_level{' '}
          {fmtNumber(race.charge_level, 3)}
          {race.potential ? ` · potential ${fmtNumber(race.potential.revenue_usd, 2)} (shortfall_usd ${fmtNumber(race.potential.shortfall_usd, 2)}, shows_potential ${String(!!race.shows_potential)})` : ''}
          {scope === null ? '' : ` · by_location.${scope} ${JSON.stringify(race.by_location?.[scope] ?? null)}`}
        </p>
      ) : null}
    </section>
  );
}
