import type { Results, StrategyResult } from '../../api/types.ts';
import { HowItWorks, Panel } from '../../components/Panel.tsx';
import { TechBlock, TechOnly, Term } from '../../components/Tech.tsx';
import {
  areaName,
  fmtCount,
  fmtLocalDate,
  fmtNumber,
  fmtUsd,
  fmtUsdLarge,
  strategyName,
  strategyTechName,
} from '../../lib/format.ts';

const ORDER = ['bands', 'forecast', 'peak'];

function labelOf(s: StrategyResult): string {
  return s.label || strategyName(s.strategy);
}

function rank(s: StrategyResult): number {
  const i = ORDER.indexOf(s.strategy);
  return i === -1 ? ORDER.length : i;
}

function sortStrategies(list: StrategyResult[]): StrategyResult[] {
  return list.toSorted((a, b) => rank(a) - rank(b));
}

export function BigNumber({ results, headline }: { results: Results; headline: StrategyResult }) {
  const cyclesPerDay = headline.cycles_per_year / 365;
  const areas = Object.entries(headline.per_location_usd ?? {});
  return (
    <Panel
      className="span-7"
      title="What one battery earns in a year"
      what={`The average for one home battery over all of ${results.year}, using the ${labelOf(headline)} method on real prices.`}
    >
      <div className="big-number">
        {fmtUsd(headline.per_battery_usd_year, 0)}
        <small>per battery per year</small>
      </div>
      <p className="big-sub">
        {fmtUsdLarge(headline.fleet_usd_year)} a year for the {fmtCount(results.fleet_size)}-battery fleet
      </p>
      <div className="fine-print">
        <span>
          {fmtUsd(headline.usd_per_kwh_year, 2)} per kWh of storage per year <TechOnly>($/kWh-year)</TechOnly>
          {' · '}
          {fmtUsd(headline.usd_per_kw_year, 2)} per kW of power per year <TechOnly>($/kW-year)</TechOnly>
        </span>
        <span>
          {fmtNumber(headline.cycles_per_year, 0)} charge cycles per year
          {cyclesPerDay > 0 ? ` (about ${fmtNumber(cyclesPerDay, 1)} full charge-and-sell a day)` : ''}
        </span>
        {areas.length ? (
          <span>
            By area:{' '}
            {areas.map(([loc, usd], i) => (
              <span key={loc}>
                {i ? ' · ' : ''}
                {areaName(loc)} {fmtUsdLarge(usd)}
              </span>
            ))}{' '}
            a year for the fleet
          </span>
        ) : null}
        <span>
          The fleet in total: {fmtNumber(results.fleet_power_mw, 0)} MW of power and {fmtNumber(results.fleet_energy_mwh, 0)}{' '}
          MWh of storage.
        </span>
      </div>
      <TechBlock
        rows={[
          ['strategy', `${headline.strategy} (${strategyTechName(headline.strategy)})`],
          ['run_id', headline.run_id],
          ['per_battery_usd_year', fmtNumber(headline.per_battery_usd_year, 2)],
          ['fleet_usd_year', fmtNumber(headline.fleet_usd_year, 0)],
          ['generated_utc', results.generated_utc],
        ]}
      />
    </Panel>
  );
}

export function ComparisonBars({ results }: { results: Results }) {
  const rows = sortStrategies(results.strategies).map((s) => ({
    key: s.strategy,
    label: labelOf(s),
    value: s.per_battery_usd_year,
    headline: s.is_headline,
    ceiling: false,
  }));
  if (results.ceiling) {
    rows.push({
      key: 'ceiling',
      label: results.ceiling.label || 'Best possible (perfect hindsight)',
      value: results.ceiling.per_battery_usd_year,
      headline: false,
      ceiling: true,
    });
  }
  const max = Math.max(1, ...rows.map((r) => r.value));
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
              {r.headline ? <span className="muted small">The headline number</span> : null}
              {r.ceiling ? <span className="muted small">Hindsight: a ceiling, not a goal</span> : null}
              <TechOnly>
                <span className="tech">{r.key}</span>
              </TechOnly>
            </span>
            <span className="bar-track" aria-hidden="true">
              <span className="bar-fill" style={{ width: `${Math.max(0, (r.value / max) * 100)}%`, display: 'block' }} />
            </span>
            <span className="bar-value">{fmtUsd(r.value, 0)}</span>
          </li>
        ))}
      </ul>
      <p className="muted small" style={{ marginTop: 10 }}>
        Dollars per battery per year, {results.year}.
      </p>
    </Panel>
  );
}

export function TopDays({ results, headline }: { results: Results; headline: StrategyResult }) {
  const days = (headline.top_days ?? []).toSorted((a, b) => b.usd - a.usd).slice(0, 10);
  const max = Math.max(1, ...days.map((d) => d.usd));
  return (
    <Panel
      className="span-7"
      title="Top 10 days by earnings"
      what={`The days in ${results.year} when the ${labelOf(headline)} method earned the most for the whole fleet. Price spikes make a few days count a lot.`}
    >
      {days.length ? (
        <ol className="days">
          {days.map((d, i) => (
            <li key={d.date_local} className="day-row">
              <span className="day-rank">{i + 1}</span>
              <span>{fmtLocalDate(d.date_local, { weekday: true })}</span>
              <span className="day-bar" style={{ width: `${Math.max(2, (d.usd / max) * 100)}%` }} aria-hidden="true" />
              <span className="bar-value">
                {fmtUsdLarge(d.usd)}
                <span className="muted small">
                  {' '}
                  ({fmtUsd(d.usd / Math.max(1, results.fleet_size), 2)} per battery)
                </span>
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="feed-empty">No daily breakdown in these results.</p>
      )}
    </Panel>
  );
}

const PLAIN_ASSUMPTIONS = [
  'Gross wholesale value at ERCOT 2025 real-time prices (ERCOT is the Texas grid operator).',
  'It leaves out retail and delivery charges, grid-support services and battery wear.',
  'The fleet is too small to move prices, so every price is replayed exactly as it happened.',
  'No peeking: each decision uses only prices already published, 15 minutes late.',
  'The AI forecast uses TimesFM-3, whose weights are licensed for non-commercial research only. A production version would use TimesFM 2.5 (Apache-2.0).',
];

export function HowWeGotThis({ results }: { results: Results }) {
  const extra = (results.assumptions ?? []).filter((a) => typeof a === 'string' && !PLAIN_ASSUMPTIONS.includes(a));
  return (
    <Panel
      title={<Term plain="The fine print" tech="backtest assumptions" />}
      what="What the number includes, what it leaves out, and why."
    >
      <HowItWorks label="How we got this number">
        <ul className="assumptions">
          {PLAIN_ASSUMPTIONS.map((a) => (
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
