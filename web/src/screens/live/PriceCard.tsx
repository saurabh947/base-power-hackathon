import { LOCATIONS, type LocationId, type Tick } from '../../api/types.ts';
import { PriceChart } from '../../components/charts/PriceChart.tsx';
import { WINDOW_LABELS, type WindowKey } from '../../components/charts/model.ts';
import { Panel } from '../../components/Panel.tsx';
import { Segmented } from '../../components/Segmented.tsx';
import { Term, TechOnly } from '../../components/Tech.tsx';
import { useLive } from '../../live/liveStore.ts';
import { areaName, fmtPriceKwh, fmtPriceMwh, fmtTimeOfDay } from '../../lib/format.ts';
import { useTechnical } from '../../state/prefs.ts';
import { ActionChip } from './ActionChip.tsx';
import { setView, useView } from './viewStore.ts';

const lastTick = (s: { history: Tick[] }) => s.history[s.history.length - 1] ?? null;

function Readouts() {
  const tick = useLive(lastTick);
  const forecastModel = useLive((s) => Object.values(s.forecast)[0]?.model ?? null);
  const forecastIssued = useLive((s) => Object.values(s.forecast)[0]?.issued_utc ?? null);
  return (
    <div className="readouts" aria-live="off">
      {LOCATIONS.map((loc) => {
        const price = tick?.price?.[loc];
        const decision = tick?.decision_price?.[loc];
        return (
          <div className="readout" key={loc}>
            <span className="readout-label">{areaName(loc)}</span>
            {typeof price === 'number' ? (
              <>
                <span className="readout-value">{fmtPriceMwh(price)}</span>
                <span className="readout-human">({fmtPriceKwh(price)})</span>
              </>
            ) : (
              <span className="readout-human">No price yet</span>
            )}
            <ActionChip action={tick?.action?.[loc]} />
            <TechOnly>
              <span className="tech">
                {loc}
                {typeof decision === 'number' ? ` · acted on ${fmtPriceMwh(decision)} (15 min late)` : ''}
              </span>
            </TechOnly>
          </div>
        );
      })}
      <TechOnly>
        <span className="tech">
          {tick ? `tick ${tick.ts_utc}` : 'no ticks yet'}
          {forecastModel ? ` · forecast ${forecastModel}, issued ${forecastIssued ? fmtTimeOfDay(forecastIssued) : '?'}` : ''}
        </span>
      </TechOnly>
    </div>
  );
}

function Legend() {
  const technical = useTechnical();
  const hasForecast = useLive((s) => Object.keys(s.forecast).length > 0);
  return (
    <ul className="legend" aria-label="Chart key">
      <li>
        <span className="swatch swatch-line" aria-hidden="true" />
        Real price
      </li>
      <li>
        <span className="swatch swatch-charge" aria-hidden="true" />▼ Charging (buying cheap power)
      </li>
      <li>
        <span className="swatch swatch-sell" aria-hidden="true" />▲ Selling
      </li>
      <li>
        <span className="swatch swatch-forecast" aria-hidden="true" />
        AI forecast, likely range{hasForecast ? '' : ' (none yet)'}
      </li>
      {technical ? (
        <li>
          <span className="swatch swatch-line dashed" aria-hidden="true" />
          <span className="tech">decision price (15-min lag) · forecast p50</span>
        </li>
      ) : null}
    </ul>
  );
}

export function PriceCard() {
  const area = useView((v) => v.area);
  const windowKey = useView((v) => v.window);
  const technical = useTechnical();
  return (
    <Panel
      title={<Term plain="Electricity price right now" tech="RT settlement point price" />}
      what="Real prices from ERCOT, the Texas grid operator. Batteries buy when power is cheap and sell when it's expensive."
      how={
        <p>
          These are the real 2025 wholesale prices for each area, in dollars per megawatt-hour ($/MWh); divide by 10
          for cents per kWh, the unit on a home power bill. Shading shows what the batteries did: teal while charging,
          amber while selling. The violet band is the AI forecast's likely range: it expects the price to land inside it
          8 times out of 10.
        </p>
      }
      actions={
        <>
          <Segmented<LocationId>
            label="Chart shows"
            value={area}
            onChange={(v) => setView({ area: v })}
            options={LOCATIONS.map((l) => ({ value: l, label: areaName(l).replace(' area', '') }))}
          />
          <Segmented<WindowKey>
            label="Time shown"
            hideLabel
            value={windowKey}
            onChange={(v) => setView({ window: v })}
            options={(['6h', '24h', 'all'] as WindowKey[]).map((k) => ({ value: k, label: WINDOW_LABELS[k] }))}
          />
        </>
      }
    >
      <Readouts />
      <Legend />
      <PriceChart area={area} windowKey={windowKey} technical={technical} />
    </Panel>
  );
}
