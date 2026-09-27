// The Results panels: the big per-battery number, the top days and the fine print, for the year shown (the
// methods compared are in "What the AI adds", ./AiGainsSection.tsx). A year with an AI-predicted part (2026)
// says so everywhere: its figures are estimates, split into what real prices earned up to real_until_utc and
// what the AI's predicted prices project after it.

import { useId } from 'react';
import type { Results, StrategyResult } from '../../api/types.ts';
import { Icon } from '../../components/Icon.tsx';
import { Panel } from '../../components/Panel.tsx';
import { TechBlock, TechOnly, Term } from '../../components/Tech.tsx';
import { fmtCount, fmtLocalDate, fmtNumber, fmtUsd, fmtUsdLarge, strategyTechName } from '../../lib/format.ts';
import { cityRows, finePrint, isProjectedDay, roundedParts, splitOf, type YearSplit } from './model.ts';

const pct = (share: number) => `${Math.round(share * 1000) / 10}%`;

/** "Real Jan 1 – Sep 20: $140 per battery · Projected Sep 21 – Dec 31: $60 · 2026 estimate: $200", in whole dollars that add up. */
function SplitLine({ year, span, real, projected }: { year: number; span: YearSplit; real: number; projected: number }) {
  const [r, p, t] = roundedParts(real, projected);
  return (
    <p className="year-split">
      <span>
        Real {span.realSpan}: <strong className="num">{fmtUsd(r, 0)}</strong> per battery
      </span>
      <span className="year-split-projected">
        <Icon name="ai" size={14} />
        Projected {span.projectedSpan}: <strong className="num">{fmtUsd(p, 0)}</strong>
      </span>
      <span>
        {year} estimate: <strong className="num">{fmtUsd(t, 0)}</strong>
      </span>
    </p>
  );
}

export function BigNumber({ results, headline, split }: { results: Results; headline: StrategyResult; split: YearSplit | null }) {
  const cyclesPerDay = headline.cycles_per_year / 365;
  const cities = cityRows(headline.per_location_usd);
  const byCityId = useId();
  const parts = split ? splitOf(headline) : null;
  const estimate = split ? ` (${results.year} estimate)` : '';
  return (
    <Panel className="span-7" title="What one battery earns in a year">
      <p className="big-number num">
        {fmtUsd(headline.per_battery_usd_year, 0)}
        <small>per battery per year</small>
        {split ? (
          <span className="chip chip-predicted big-number-chip">
            <Icon name="ai" size={13} />
            {results.year} estimate
          </span>
        ) : null}
      </p>
      {split && parts ? <SplitLine year={results.year} span={split} {...parts} /> : null}
      <p className="big-sub">
        {fmtUsdLarge(headline.fleet_usd_year)} a year for the {fmtCount(results.fleet_size)}-battery fleet{estimate}
      </p>

      <ul className="result-stats">
        <li className="result-stat">
          <strong className="stat-value">{fmtUsd(headline.usd_per_kwh_year, 2)}</strong>{' '}
          <span className="stat-label">
            per kWh of storage per year <TechOnly>($/kWh-year)</TechOnly>
          </span>
        </li>
        <li className="result-stat">
          <strong className="stat-value">{fmtUsd(headline.usd_per_kw_year, 2)}</strong>{' '}
          <span className="stat-label">
            per kW of power per year <TechOnly>($/kW-year)</TechOnly>
          </span>
        </li>
        <li className="result-stat">
          <strong className="stat-value">{fmtNumber(headline.cycles_per_year, 0)}</strong>{' '}
          <span className="stat-label">
            charge cycles per year
            {cyclesPerDay > 0 ? <span className="stat-note"> (about {fmtNumber(cyclesPerDay, 1)} full charge-and-sell a day)</span> : null}
          </span>
        </li>
      </ul>

      {cities.length ? (
        <section aria-labelledby={byCityId}>
          <h3 className="result-section-label" id={byCityId}>
            By city <span className="label-note">what each city's batteries earn in a year{split ? `, ${results.year} estimate` : ''}</span>
          </h3>
          <ul className="city-results">
            {cities.map((c) => (
              <li key={c.location} className="city-result">
                <span className="city-result-name">
                  {c.name}
                  <TechOnly>
                    <span className="tech"> {c.location}</span>
                  </TechOnly>
                </span>
                <span className="city-result-bar" aria-hidden="true">
                  <span style={{ width: pct(c.share) }} />
                </span>
                <span className="city-result-value">{fmtUsdLarge(c.usd, 2)}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <p className="fleet-size-note">
        The fleet in total: {fmtNumber(results.fleet_power_mw, 0)} MW of power and {fmtNumber(results.fleet_energy_mwh, 0)}{' '}
        MWh of storage.
      </p>
      <TechBlock
        rows={[
          ['strategy', `${headline.strategy} (${strategyTechName(headline.strategy)})`],
          ['run_id', headline.run_id],
          ['per_battery_usd_year', fmtNumber(headline.per_battery_usd_year, 2)],
          ['fleet_usd_year', fmtNumber(headline.fleet_usd_year, 0)],
          ['per_location_usd', JSON.stringify(headline.per_location_usd ?? {})],
          ...(parts
            ? ([
                ['real_until_utc', String(results.real_until_utc)],
                ['real_per_battery_usd', fmtNumber(parts.real, 2)],
                ['projected_per_battery_usd', fmtNumber(parts.projected, 2)],
              ] as [string, string][])
            : []),
          ['generated_utc', results.generated_utc],
        ]}
      />
    </Panel>
  );
}

export function TopDays({ results, headline, split }: { results: Results; headline: StrategyResult; split: YearSplit | null }) {
  const days = (headline.top_days ?? []).toSorted((a, b) => b.usd - a.usd).slice(0, 10);
  const max = Math.max(1, ...days.map((d) => d.usd));
  return (
    <Panel className="span-7" title="Top 10 days by earnings">
      {days.length ? (
        <ol className="days">
          {days.map((d, i) => {
            const projected = isProjectedDay(d.date_local, split);
            return (
              <li key={d.date_local} className={`day-row${projected ? ' is-projected' : ''}`}>
                <span className="day-rank">{i + 1}</span>
                <span className="day-date">
                  {fmtLocalDate(d.date_local, { weekday: true })}
                  {projected ? <span className="day-projected">projected</span> : null}
                </span>
                <span className="day-bar" style={{ width: pct(Math.max(0.02, d.usd / max)) }} aria-hidden="true" />
                <span className="day-value">
                  {fmtUsdLarge(d.usd)}
                  <span className="muted small"> ({fmtUsd(d.usd / Math.max(1, results.fleet_size), 2)} per battery)</span>
                </span>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="feed-empty">No daily breakdown in these results.</p>
      )}
    </Panel>
  );
}

export function HowWeGotThis({ results, split }: { results: Results; split: YearSplit | null }) {
  const notes = (results.assumptions ?? []).filter((a) => typeof a === 'string');
  return (
    <Panel className="span-5" title={<Term plain="The fine print" tech="backtest assumptions" />}>
      <ul className="assumptions">
        {finePrint(results, split).map((a) => (
          <li key={a}>{a}</li>
        ))}
      </ul>
      {notes.length ? (
        <TechOnly>
          <p className="muted small">The simulator's own notes for this run:</p>
          <ul className="assumptions">
            {notes.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        </TechOnly>
      ) : null}
    </Panel>
  );
}
