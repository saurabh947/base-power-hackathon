// "What the AI adds" (Results, right under the headline row): what the AI forecast earns over the simple
// rule in the year shown, then three plain-language cards: earns more, gets closer to perfect, soaks up
// surplus power. In a year with an AI-predicted part (2026) the figures are estimates, and the gain is split
// into its real and projected parts. The numbers and words come from ./aiGains.ts.

import type { ReactNode } from 'react';
import { Icon, type IconName } from '../../components/Icon.tsx';
import { Panel } from '../../components/Panel.tsx';
import { TechBlock } from '../../components/Tech.tsx';
import { fmtCount, fmtNumber, lowerFirst, shortLabel } from '../../lib/format.ts';
import { gainsView, type EarnsCard, type GainsSplit, type PerfectCard, type PitchGains, type SplitInput, type SurplusCard } from './aiGains.ts';

const width = (share: number) => `${Math.round(Math.min(1, Math.max(0, share)) * 1000) / 10}%`;

interface Row {
  key: string;
  label: string;
  value: string;
  share: number;
  ai: boolean;
}

/** Labelled bars, the AI's in green. `battery` draws them as battery gauges (for charge levels). */
function Rows({ rows, battery = false }: { rows: Row[]; battery?: boolean }) {
  return (
    <ul className={`gains-rows${battery ? ' is-battery' : ''}`}>
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
  );
}

/** Every card has the same four parts, so they line up across the row: head, figure (bars and caption), details, meaning. */
function Card({
  icon,
  title,
  tag,
  rows,
  caption,
  details,
  means,
}: {
  icon: IconName;
  title: string;
  tag?: ReactNode;
  rows: ReactNode;
  caption: ReactNode;
  details?: ReactNode;
  means: ReactNode;
}) {
  return (
    <li className="gains-card">
      <h3 className="gains-card-head">
        <Icon name={icon} size={16} />
        <span>{title}</span>
        {tag}
      </h3>
      <div className="gains-figure">
        {rows}
        <p className="gains-caption">{caption}</p>
      </div>
      <div className="gains-details">{details}</div>
      <p className="gains-means">
        <strong>What this means:</strong> {means}
      </p>
    </li>
  );
}

function Earns({ card, split, estimate }: { card: EarnsCard; split: GainsSplit | null; estimate: string }) {
  return (
    <Card
      icon="dollar"
      title="Earns more"
      rows={
        <Rows
          rows={[
            { key: 'ai', label: card.ai.short, value: card.aiText, share: card.aiShare, ai: true },
            { key: 'base', label: card.base.short, value: card.baseText, share: card.baseShare, ai: false },
          ]}
        />
      }
      caption={`Earned per battery per year${estimate}.`}
      details={
        <>
          {split ? (
            <>
              <p>{split.earnsReal}</p>
              <p className="year-split-projected">
                <Icon name="ai" size={13} />
                {split.earnsProjected}
              </p>
            </>
          ) : null}
          {card.fleetDiffText ? (
            <p>
              Whole fleet: <strong className="gains-plus">{card.fleetDiffText}</strong>
              {card.fleetText ? <span className="muted"> ({card.fleetText})</span> : null}
            </p>
          ) : null}
        </>
      }
      means="Same batteries, same prices: the AI picks better times to buy and to sell."
    />
  );
}

function Perfect({ card, estimate }: { card: PerfectCard; estimate: string }) {
  return (
    <Card
      icon="target"
      title="Gets closer to perfect"
      rows={<Rows rows={card.bars.map((b) => ({ key: b.key, label: b.label, value: b.text, share: b.share, ai: b.ai }))} />}
      caption={
        <>
          Share of the best possible earnings{card.bestText ? <>, where 100% is {card.bestText} per battery per year</> : null}
          {estimate}.
        </>
      }
      means="“Perfect” means knowing every price in advance, which no real system can do."
    />
  );
}

function Surplus({ card, estimate }: { card: SurplusCard; estimate: string }) {
  return (
    <Card
      icon="sun"
      title="Soaks up surplus power"
      rows={
        <Rows
          rows={[
            { key: 'ai', label: card.ai.short, value: card.aiText, share: card.aiShare, ai: true },
            { key: 'base', label: card.base.short, value: card.baseText, share: card.baseShare, ai: false },
          ]}
        />
      }
      caption={`Power bought at $0/MWh or less in the year${estimate}.`}
      details={
        <>
          {card.times ? (
            <p>
              <strong className="gains-plus">{card.times}</strong> as much as the {lowerFirst(card.base.short)}.
            </p>
          ) : null}
          {card.homes ? <p className="muted">The AI’s {card.aiText} is {card.homes}.</p> : null}
        </>
      }
      means="It bought power when it was free or cheaper than free, often excess wind or solar."
    />
  );
}

export function AiGainsSection({ gains, fleetSize, split }: { gains: PitchGains; fleetSize: number | null; split: SplitInput | null }) {
  const view = gainsView(gains, fleetSize, split);
  const { headline } = view;
  const aiLabel = view.year.strategies[gains.ai].label;
  const baseLabel = view.year.strategies[gains.baseline].label;
  const batteries = fleetSize ? `${fmtCount(fleetSize)} ` : '';
  // A year with an AI-predicted part: its figures are estimates, part projected.
  const estimate = split ? ` (${view.year.year} estimate)` : '';

  return (
    <Panel
      className="span-12 ai-gains"
      title="What the AI adds"
      what={
        split
          ? `The ${shortLabel(aiLabel)} against the ${lowerFirst(shortLabel(baseLabel))} over the full year ${view.year.year}: the same ${batteries}batteries and the same prices, real through ${split.span.realSpan.split(' – ')[1]} and the AI’s prediction after that. Only the decisions differ.`
          : `The ${shortLabel(aiLabel)} against the ${lowerFirst(shortLabel(baseLabel))} over the full year ${view.year.year}: the same ${batteries}batteries and the same real prices. Only the decisions differ.`
      }
      how={
        <p>
          Both methods replay every 5-minute price of the year with the same code as the live fleet. “Surplus power”
          is power bought at a price of $0/MWh or less.
        </p>
      }
    >
      {headline ? (
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
          {view.split ? (
            <p className="year-split">
              <span>{view.split.real}</span>
              <span className="year-split-projected">
                <Icon name="ai" size={14} />
                {view.split.projected}
              </span>
              <span>{view.split.estimate}</span>
            </p>
          ) : null}
          <p className="sr-only">
            {view.year.year}
            {split ? ' estimate' : ''}: {headline.sentence}.
          </p>
        </div>
      ) : null}

      <ul className="gains-cards">
        {view.earns ? <Earns card={view.earns} split={view.split} estimate={estimate} /> : null}
        {view.perfect ? <Perfect card={view.perfect} estimate={estimate} /> : null}
        {view.surplus ? <Surplus card={view.surplus} estimate={estimate} /> : null}
      </ul>

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
    </Panel>
  );
}
