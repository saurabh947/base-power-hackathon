// The Results panels: the big per-battery number, the methods compared, the top days and the fine print, for
// the year shown. A year with an AI-predicted part (2026) says so everywhere: its figures are estimates, split
// into what real prices earned up to real_until_utc and what the AI's predicted prices project after it.

import { useId } from 'react';
import type { Results, StrategyResult } from '../../api/types.ts';
import { Icon } from '../../components/Icon.tsx';
import { HowItWorks, Panel } from '../../components/Panel.tsx';
import { TechBlock, TechOnly, Term } from '../../components/Tech.tsx';
import { fmtCount, fmtLocalDate, fmtNumber, fmtUsd, fmtUsdLarge, strategyTechName } from '../../lib/format.ts';
import { cityRows, comparisonRows, isProjectedDay, roundedParts, splitOf, strategyLabel, type YearSplit } from './model.ts';

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

/** "$98 real + $33 projected", adding up to the bar's value. */
function barSplit(real: number, projected: number): string {
  const [r, p] = roundedParts(real, projected);
  return `${fmtUsd(r, 0)} real + ${fmtUsd(p, 0)} projected`;
}

export function BigNumber({ results, headline, split }: { results: Results; headline: StrategyResult; split: YearSplit | null }) {
  const cyclesPerDay = headline.cycles_per_year / 365;
  const cities = cityRows(headline.per_location_usd);
  const byCityId = useId();
  const parts = split ? splitOf(headline) : null;
  const estimate = split ? ` (${results.year} estimate)` : '';
  return (
    <Panel
      className="span-7"
      title="What one battery earns in a year"
      what={
        split
          ? `The average for one home battery over all of ${results.year}, using the ${strategyLabel(headline)} method: real prices through ${fmtLocalDate(split.lastRealDay).replace(/, \d{4}$/, '')}, then the AI's predicted prices, so part of it is projected.`
          : `The average for one home battery over all of ${results.year}, using the ${strategyLabel(headline)} method on real prices.`
      }
    >
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

export function ComparisonBars({ results, headline, split }: { results: Results; headline: StrategyResult; split: YearSplit | null }) {
  const rows = comparisonRows(results, headline);
  return (
    <Panel
      className="span-5"
      title="How the decision methods compare"
      what="Best possible assumes perfect knowledge of future prices. No real system can reach it."
      how={
        <p>
          Each bar is the average earned per battery per year. The simple rule buys in the cheapest quarter of the past
          week and sells in the priciest tenth; the evening peak method charges 10 am to 3 pm and sells 6 pm to 9 pm;
          the AI forecast saves its charge for the hours it expects to be most expensive.
        </p>
      }
    >
      <ul className="bars">
        {rows.map((r) => (
          <li key={r.key} className={`bar-row${r.headline ? ' headline' : ''}${r.ceiling ? ' ceiling' : ''}`}>
            <span className="bar-label">
              <strong>{r.label}</strong>
              {r.headline ? <span className="bar-note">The headline number</span> : null}
              {r.ceiling ? <span className="bar-note">Hindsight: a ceiling, not a goal</span> : null}
              <TechOnly>
                <span className="tech">{r.key}</span>
              </TechOnly>
            </span>
            <span className="bar-value num">
              {fmtUsd(r.value, 0)}
              {r.split ? <span className="bar-split">{barSplit(r.split.real, r.split.projected)}</span> : null}
            </span>
            <span className="bar-track" aria-hidden="true">
              {r.split ? (
                <>
                  <span className="bar-fill" style={{ width: pct(r.split.realShare) }} />
                  <span className="bar-fill bar-projected" style={{ width: pct(r.split.projectedShare) }} />
                </>
              ) : (
                <span className="bar-fill" style={{ width: pct(r.share) }} />
              )}
            </span>
          </li>
        ))}
      </ul>
      <p className="bars-note">
        Dollars per battery per year, {results.year}
        {split ? (
          <>
            : the solid part on real prices ({split.realSpan}), the violet part <span className="year-split-projected">projected</span> on
            the AI’s predicted prices ({split.projectedSpan}).
          </>
        ) : (
          '.'
        )}
      </p>
    </Panel>
  );
}

export function TopDays({ results, headline, split }: { results: Results; headline: StrategyResult; split: YearSplit | null }) {
  const days = (headline.top_days ?? []).toSorted((a, b) => b.usd - a.usd).slice(0, 10);
  const max = Math.max(1, ...days.map((d) => d.usd));
  return (
    <Panel
      className="span-7"
      title="Top 10 days by earnings"
      what={`The days in ${results.year} when the ${strategyLabel(headline)} method earned the most for the whole fleet. Price spikes make a few days count a lot.${split ? ` Days from ${split.projectedSpan.split(' – ')[0]} on are projected, on the AI's predicted prices.` : ''}`}
    >
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

const PLAIN_ASSUMPTIONS = [
  'It leaves out retail and delivery charges, grid-support services and battery wear.',
  'The fleet is too small to move prices, so every price is replayed exactly as it happened.',
  'No peeking: each decision uses only prices already published, 15 minutes late.',
  'The AI forecast uses TimesFM-3, whose weights are licensed for non-commercial research only. A production version would use TimesFM 2.5 (Apache-2.0).',
];

/** The first line of the fine print: whose prices, for the year shown. */
function pricesLine(results: Results, split: YearSplit | null): string {
  if (!split) return `Gross wholesale value at ERCOT ${results.year} real-time prices (ERCOT is the Texas grid operator).`;
  return `Gross wholesale value at ERCOT ${results.year} real-time prices through ${fmtLocalDate(split.lastRealDay)} (ERCOT is the Texas grid operator), then at the AI's predicted prices (TimesFM-3) for the rest of the year. That second part is a projection, not a result.`;
}

export function HowWeGotThis({ results, split }: { results: Results; split: YearSplit | null }) {
  const plain = [pricesLine(results, split), ...PLAIN_ASSUMPTIONS];
  const extra = (results.assumptions ?? []).filter((a) => typeof a === 'string' && !plain.includes(a));
  return (
    <Panel
      title={<Term plain="The fine print" tech="backtest assumptions" />}
      what="What the number includes, what it leaves out, and why."
    >
      <HowItWorks label="How we got this number">
        <ul className="assumptions">
          {plain.map((a) => (
            <li key={a}>{a}</li>
          ))}
        </ul>
        {extra.length ? (
          <>
            <p className="muted small">The simulator's own notes for this run:</p>
            <ul className="assumptions">
              {extra.map((a) => (
                <li key={a}>{a}</li>
              ))}
            </ul>
          </>
        ) : null}
      </HowItWorks>
    </Panel>
  );
}
