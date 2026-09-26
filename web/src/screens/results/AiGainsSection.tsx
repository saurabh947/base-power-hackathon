// "What the AI adds" (Results, right under the headline row): what the AI forecast earns over the simple
// rule, then four plain-language cards. A switch picks the year for the headline and the first three
// cards; the spike card always shows 2024 and says so. The numbers and words come from ./aiGains.ts.

import { useState, type ReactNode } from 'react';
import type { AiGains } from '../../api/types.ts';
import { Icon, type IconName } from '../../components/Icon.tsx';
import { Panel } from '../../components/Panel.tsx';
import { Segmented } from '../../components/Segmented.tsx';
import { TechBlock } from '../../components/Tech.tsx';
import { fmtCount, fmtNumber, lowerFirst, shortLabel } from '../../lib/format.ts';
import {
  gainsView,
  SPIKE_YEAR,
  yearOptions,
  yearsNote,
  type EarnsCard,
  type PerfectCard,
  type SpikeCard,
  type SurplusCard,
} from './aiGains.ts';

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

function Earns({ card }: { card: EarnsCard }) {
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
      caption="Earned per battery per year."
      details={
        card.fleetDiffText ? (
          <p>
            Whole fleet: <strong className="gains-plus">{card.fleetDiffText}</strong>
            {card.fleetText ? <span className="muted"> ({card.fleetText})</span> : null}
          </p>
        ) : null
      }
      means="Same batteries, same prices: the AI picks better times to buy and to sell."
    />
  );
}

function Perfect({ card }: { card: PerfectCard }) {
  return (
    <Card
      icon="target"
      title="Gets closer to perfect"
      rows={<Rows rows={card.bars.map((b) => ({ key: b.key, label: b.label, value: b.text, share: b.share, ai: b.ai }))} />}
      caption={
        <>
          Share of the best possible earnings{card.bestText ? <>, where 100% is {card.bestText} per battery per year</> : null}.
        </>
      }
      means="“Perfect” means knowing every price in advance, which no real system can do."
    />
  );
}

function Surplus({ card }: { card: SurplusCard }) {
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
      caption="Power bought at $0/MWh or less in the year."
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

function Spikes({ card }: { card: SpikeCard }) {
  return (
    <Card
      icon="bolt"
      title="Ready for spikes"
      tag={
        <span className="gains-year-tag">
          <span className="sr-only">, in </span>
          {card.year}
        </span>
      }
      rows={
        <Rows
          battery
          rows={[
            { key: 'ai', label: card.ai.short, value: card.aiChargeText, share: card.aiCharge, ai: true },
            { key: 'base', label: card.base.short, value: card.baseChargeText, share: card.baseCharge, ai: false },
          ]}
        />
      }
      caption="Charge left when the price spikes began, on average."
      details={
        <>
          <p>
            Delivered <strong>{card.aiMwhText}</strong> vs {card.baseMwhText} during the spikes
            {card.times ? (
              <>
                {' '}
                (<strong className="gains-plus">{card.times}</strong>)
              </>
            ) : null}
            .
          </p>
          {card.eventsText ? <p className="muted">{card.eventsText}</p> : null}
        </>
      }
      means="The AI kept charge for the expensive moments, so it had more to sell when prices jumped."
    />
  );
}

/** Technical details: the raw numbers behind the spike card (always 2024), AI / baseline. */
function spikeRows(gains: AiGains): [string, string][] {
  const y = gains.years.find((x) => x.year === SPIKE_YEAR);
  if (!y) return [];
  const a = y.strategies[gains.ai];
  const b = y.strategies[gains.baseline];
  return [
    [
      `spike card (${SPIKE_YEAR})`,
      `spike_events ${fmtNumber(a.spike_events, 0)} · spike_charge_level ${fmtNumber(a.spike_charge_level, 3)} / ${fmtNumber(b.spike_charge_level, 3)} · spike_mwh ${fmtNumber(a.spike_mwh, 1)} / ${fmtNumber(b.spike_mwh, 1)}`,
    ],
  ];
}

export function AiGainsSection({ gains, fleetSize }: { gains: AiGains; fleetSize: number | null }) {
  const options = yearOptions(gains);
  const [year, setYear] = useState<number>(options[0].year);
  const view = gainsView(gains, year, fleetSize);
  const note = yearsNote(gains);
  const { headline } = view;
  const aiLabel = view.year.strategies[gains.ai].label;
  const baseLabel = view.year.strategies[gains.baseline].label;
  const batteries = fleetSize ? `${fmtCount(fleetSize)} ` : '';

  return (
    <Panel
      className="span-12 ai-gains"
      title="What the AI adds"
      what={`The ${shortLabel(aiLabel)} against the ${lowerFirst(shortLabel(baseLabel))} over a full year: the same ${batteries}batteries and the same real prices. Only the decisions differ.`}
      how={
        <p>
          Both methods replay every 5-minute price of the year with the same code as the live fleet. “Surplus power”
          is power bought at a price of $0/MWh or less; a “spike” is a 5-minute price of $1,000/MWh or more.
        </p>
      }
      actions={
        options.length > 1 ? (
          <Segmented
            label="Year"
            value={String(view.year.year)}
            options={options.map((o) => ({ value: String(o.year), label: o.label }))}
            onChange={(v) => setYear(Number(v))}
          />
        ) : null
      }
    >
      {headline ? (
        <div className="gains-headline">
          <p className="gains-kicker">
            {aiLabel}, {view.year.year}
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
          <p className="sr-only" aria-live="polite">
            {view.year.year}: {headline.sentence}.
          </p>
          {note ? <p className="gains-note">{note}</p> : null}
        </div>
      ) : null}

      <ul className="gains-cards">
        {view.earns ? <Earns card={view.earns} /> : null}
        {view.perfect ? <Perfect card={view.perfect} /> : null}
        {view.surplus ? <Surplus card={view.surplus} /> : null}
        {view.spike ? <Spikes card={view.spike} /> : null}
      </ul>

      <TechBlock
        rows={[
          ['GET /results/ai-gains', `ai=${gains.ai} · baseline=${gains.baseline} · years ${gains.years.map((y) => y.year).join(', ')}`],
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
          ...spikeRows(gains),
        ]}
      />
    </Panel>
  );
}
