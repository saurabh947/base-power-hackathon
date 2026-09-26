import { AnimatedNumber } from '../../components/AnimatedNumber.tsx';
import { Panel } from '../../components/Panel.tsx';
import { TechBlock } from '../../components/Tech.tsx';
import { useLive } from '../../live/liveStore.ts';
import { fmtEnergy, fmtHomesDay, fmtNumber, fmtUsd } from '../../lib/format.ts';
import { RaceBars } from './RaceBars.tsx';

const formatMoney = (n: number) => fmtUsd(n, 2);

export function MoneyCard() {
  const totals = useLive((s) => s.totals);
  const received = useLive((s) => s.received);
  const fleetSize = useLive((s) => s.settings?.fleet_size ?? null);
  const perBattery = fleetSize ? totals.revenue_usd / fleetSize : null;
  return (
    <Panel
      title="Earned so far in this replay"
      what="Gross wholesale value: what the power the fleet sold was worth, minus what the power it bought cost."
      how={
        <p>
          Every 5 minutes the simulator multiplies the power each area delivered by that moment's real price. It is
          gross wholesale value: it leaves out retail and delivery charges, grid-support services and battery wear.
          It can dip below zero while the batteries are buying, and climbs when they sell.
        </p>
      }
    >
      <div className="money-value">
        {/* Mount the counter only once real data is here, so it never counts up from an empty $0. */}
        {received ? (
          <>
            <span aria-hidden="true">
              <AnimatedNumber value={totals.revenue_usd} format={formatMoney} />
            </span>
            <span className="sr-only">{fmtUsd(totals.revenue_usd, 2)}</span>
          </>
        ) : (
          <span className="muted">–</span>
        )}
      </div>
      <div className="money-sub">
        {perBattery !== null ? (
          <span>
            That is <strong>{fmtUsd(perBattery, perBattery !== 0 && Math.abs(perBattery) < 1 ? 3 : 2)}</strong> per battery
            so far.
          </span>
        ) : null}
        <span>
          Sold <strong>{fmtEnergy(totals.sold_kwh)}</strong> · Bought <strong>{fmtEnergy(totals.bought_kwh)}</strong>
        </span>
        {totals.sold_kwh > 0 ? <span>Power sold: {fmtHomesDay(totals.sold_kwh)}.</span> : null}
        {totals.revenue_usd < 0 ? (
          <span>Below zero means the fleet has spent more buying power than it has earned so far.</span>
        ) : null}
      </div>
      <RaceBars scope={null} variant="card" />
      <TechBlock
        rows={[
          ['revenue_usd', fmtNumber(totals.revenue_usd, 4)],
          ['sold_kwh', fmtNumber(totals.sold_kwh, 1)],
          ['bought_kwh', fmtNumber(totals.bought_kwh, 1)],
          ['ticks', `${totals.ticks} (5-minute steps)`],
        ]}
      />
    </Panel>
  );
}
