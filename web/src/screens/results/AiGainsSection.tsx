// "What the AI adds" (Results, beside the big number): what the AI forecast earns over the simple rule in the
// year shown, every decision method's bar with its share of the best possible, then the surplus power the AI
// soaked up. In a year with an AI-predicted part (2026) the figures are estimates: the uplift and each bar are
// split into their real and projected parts. The headline and surplus words come from ./aiGains.ts (GET
// /results/ai-gains?year=; without it the bars show alone), the bars from ./model.ts (GET /results/pitch?year=).

import { useId } from 'react';
import type { Results, StrategyResult } from '../../api/types.ts';
import { Icon } from '../../components/Icon.tsx';
import { Panel } from '../../components/Panel.tsx';
import { TechBlock, TechOnly } from '../../components/Tech.tsx';
import { fmtNumber, fmtPercent, fmtUsd, lowerFirst } from '../../lib/format.ts';
import { gainsView, type GainsView, type PitchGains, type SplitInput, type SurplusCard } from './aiGains.ts';
import { comparisonRows, roundedParts, splitOf, type BarRow, type YearSplit } from './model.ts';

const width = (share: number) => `${Math.round(Math.min(1, Math.max(0, share)) * 1000) / 10}%`;

/** For a split year: the AI's and the baseline's real and projected parts, from the results. */
function splitInput(results: Results, span: YearSplit | null, gains: PitchGains): SplitInput | null {
  if (!span) return null;
  const ai = results.strategies.find((s) => s.strategy === gains.ai);
  const base = results.strategies.find((s) => s.strategy === gains.baseline);
  const a = ai ? splitOf(ai) : null;
  const b = base ? splitOf(base) : null;
  return a && b ? { span, ai: a, base: b } : null;
}

/** "$98 real + $33 projected", adding up to the bar's value. */
function barSplit(real: number, projected: number): string {
  const [r, p] = roundedParts(real, projected);
  return `${fmtUsd(r, 0)} real + ${fmtUsd(p, 0)} projected`;
}

function Headline({ view, aiLabel }: { view: GainsView; aiLabel: string }) {
  const { headline, split } = view;
  if (!headline) return null;
  return (
    <div className="gains-headline">
      <p className="gains-kicker">
        {aiLabel}, {view.year.year}
        {split ? ' estimate' : ''}
      </p>
      <p className="gains-big num">
        <span>{headline.amount}</span>
        <small>per battery per year</small>
        {headline.pctText ? <span className="gains-pct">{headline.pctText}</span> : null}
      </p>
      <p className="gains-sub">
        {headline.versus}
        {headline.fleetText ? `, ${headline.fleetText}` : ''}
      </p>
      {split ? (
        <p className="year-split">
          <span>{split.real}</span>
          <span className="year-split-projected">
            <Icon name="ai" size={14} />
            {split.projected}
          </span>
          <span>{split.estimate}</span>
        </p>
      ) : null}
      <p className="sr-only">
        {view.year.year}
        {split ? ' estimate' : ''}: {headline.sentence}.
      </p>
    </div>
  );
}

/** Every method per battery per year, the AI's green, with its share of the best possible; the ceiling hatched. */
function MethodBars({ rows }: { rows: BarRow[] }) {
  return (
    <ul className="bars">
      {rows.map((r) => (
        <li key={r.key} className={`bar-row${r.headline ? ' headline' : ''}${r.ceiling ? ' ceiling' : ''}`}>
          <span className="bar-label">
            <strong>{r.label}</strong>
            {r.ofBest !== null ? <span className="bar-note">{fmtPercent(r.ofBest, 0)} of best possible</span> : null}
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
                <span className="bar-fill" style={{ width: width(r.split.realShare) }} />
                <span className="bar-fill bar-projected" style={{ width: width(r.split.projectedShare) }} />
              </>
            ) : (
              <span className="bar-fill" style={{ width: width(r.share) }} />
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Surplus({ card, estimate }: { card: SurplusCard; estimate: string }) {
  const id = useId();
  const rows = [
    { key: 'ai', label: card.ai.short, value: card.aiText, share: card.aiShare, ai: true },
    { key: 'base', label: card.base.short, value: card.baseText, share: card.baseShare, ai: false },
  ];
  return (
    <section className="gains-surplus" aria-labelledby={id}>
      <h3 className="result-section-label" id={id}>
        Surplus power soaked up <span className="label-note">bought at $0/MWh or less{estimate}</span>
      </h3>
      <ul className="gains-rows">
        {rows.map((r) => (
          <li key={r.key} className={`gains-row${r.ai ? ' is-ai' : ''}`}>
            <span className="gains-row-label">{r.label}</span>
            <span className="gains-row-track" aria-hidden="true">
              <span className="gains-row-fill" style={{ width: width(r.share) }} />
            </span>
            <span className="gains-row-value num">{r.value}</span>
          </li>
        ))}
      </ul>
      {card.times ? (
        <p className="gains-surplus-times">
          <strong className="gains-plus">{card.times}</strong> as much as the {lowerFirst(card.base.short)}.
        </p>
      ) : null}
    </section>
  );
}

export function AiGainsSection({
  results,
  headline,
  split,
  gains,
}: {
  results: Results;
  headline: StrategyResult;
  split: YearSplit | null;
  /** GET /results/ai-gains for the year shown; null: the bars show alone. */
  gains: PitchGains | null;
}) {
  const rows = comparisonRows(results, headline);
  const view = gains ? gainsView(gains, results.fleet_size, splitInput(results, split, gains)) : null;
  // A year with an AI-predicted part: its figures are estimates, part projected.
  const estimate = split ? `, ${results.year} estimate` : '';

  return (
    <Panel
      className="span-5 ai-gains"
      title="What the AI adds"
      how={
        <p>
          Each bar is the average earned per battery per year. The simple rule buys in the cheapest quarter of the past
          week and sells in the priciest tenth; the evening peak method charges 10 am to 3 pm and sells 6 pm to 9 pm;
          the AI forecast saves its charge for the hours it expects to be most expensive.
        </p>
      }
    >
      {view && gains ? <Headline view={view} aiLabel={view.year.strategies[gains.ai].label} /> : null}
      <MethodBars rows={rows} />
      {view?.surplus ? <Surplus card={view.surplus} estimate={estimate} /> : null}

      {view && gains ? (
        <TechBlock
          rows={[
            ['GET /results/ai-gains', `ai=${gains.ai} · baseline=${gains.baseline} · year=${view.year.year}`],
            ['year', `${view.year.year}${view.year.is_pitch ? ' (is_pitch)' : ''}`],
            ['uplift', JSON.stringify(view.year.uplift)],
            ['best_possible_per_battery_usd_year', fmtNumber(view.year.best_possible_per_battery_usd_year, 2)],
            ...Object.entries(view.year.strategies).map(
              ([id, s]) =>
                [
                  id,
                  `${fmtNumber(s.per_battery_usd_year, 2)} $/battery-yr · share_of_best ${fmtNumber(s.share_of_best, 3)} · surplus_mwh ${fmtNumber(s.surplus_mwh, 1)}`,
                ] as [string, string],
            ),
          ]}
        />
      ) : null}
    </Panel>
  );
}
