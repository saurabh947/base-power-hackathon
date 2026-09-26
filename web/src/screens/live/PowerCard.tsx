import { LOCATIONS, type Tick } from '../../api/types.ts';
import { PowerChart } from '../../components/charts/PowerChart.tsx';
import { Icon } from '../../components/Icon.tsx';
import { Panel } from '../../components/Panel.tsx';
import { TechBlock, Term } from '../../components/Tech.tsx';
import { useLive } from '../../live/liveStore.ts';
import {
  areaName,
  fmtHomesPower,
  fmtNumber,
  fmtPercent,
  fmtPercentValue,
  fmtPower,
  fmtPowerFlow,
} from '../../lib/format.ts';
import { useView } from './viewStore.ts';

const lastTick = (s: { history: Tick[] }) => s.history[s.history.length - 1] ?? null;

function fleet(v: Partial<Record<string, number>> | undefined): number | null {
  if (!v) return null;
  if (typeof v.fleet === 'number') return v.fleet;
  const parts = LOCATIONS.map((l) => v[l]).filter((n): n is number => typeof n === 'number');
  return parts.length ? parts.reduce((a, b) => a + b, 0) : null;
}

/** 5% is the spec's tracking target: within it the fleet is "on target". */
const ON_TARGET_PCT = 5;

function Readouts() {
  const tick = useLive(lastTick);
  if (!tick) return <p className="feed-empty">No power data yet.</p>;
  const target = fleet(tick.target_kw);
  const actual = fleet(tick.actual_kw);
  const gapKw = target !== null && actual !== null ? Math.abs(target - actual) : null;
  const onTarget = typeof tick.error_pct === 'number' ? tick.error_pct <= ON_TARGET_PCT : gapKw !== null && gapKw < 1000;
  return (
    <div className="readouts">
      <div className="readout">
        <span className="readout-label">
          <Term plain="Asked for" tech="target kW" />
        </span>
        <span className="readout-value">{target === null ? '–' : fmtPowerFlow(target)}</span>
        {target !== null && Math.abs(target) >= 1 ? (
          <span className="readout-human">({fmtHomesPower(target)})</span>
        ) : null}
      </div>
      <div className="readout">
        <span className="readout-label">
          <Term plain="Delivered" tech="actual kW" />
        </span>
        <span className="readout-value">{actual === null ? '–' : fmtPowerFlow(actual)}</span>
        {onTarget ? (
          <span className="chip chip-ok">
            <Icon name="check" size={14} />
            On target
          </span>
        ) : (
          <span className="chip chip-warn">
            <Icon name="alert" size={14} />
            Catching up{gapKw !== null ? `: ${fmtPower(gapKw)} short` : ''}
          </span>
        )}
      </div>
      <TechBlock
        rows={[
          ['error_pct', `${fmtPercentValue(tick.error_pct)} (target: within ${ON_TARGET_PCT}%)`],
          ['target_kw.fleet', target === null ? '–' : `${fmtNumber(target, 1)} kW`],
          ['actual_kw.fleet', actual === null ? '–' : `${fmtNumber(actual, 1)} kW`],
          ...LOCATIONS.map(
            (l) =>
              [
                `${l} target / actual`,
                `${fmtNumber(tick.target_kw?.[l] ?? NaN, 1)} / ${fmtNumber(tick.actual_kw?.[l] ?? NaN, 1)} kW`,
              ] as [string, string],
          ),
          ['healthy_workers', String(tick.healthy_workers)],
        ]}
      />
    </div>
  );
}

function ChargeMeter() {
  const tick = useLive(lastTick);
  const level = tick?.charge_level?.fleet ?? null;
  if (typeof level !== 'number') return null;
  return (
    <>
      <div className="meter">
        <span>
          <Term plain="Average charge level" tech="fleet SoC" />
        </span>
        <span
          className="meter-track"
          role="meter"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(level * 100)}
          aria-label="Average charge level"
        >
          <span className="meter-fill" style={{ width: `${Math.max(0, Math.min(1, level)) * 100}%`, display: 'block' }} />
        </span>
        <strong className="num">{fmtPercent(level)}</strong>
      </div>
      <TechBlock
        rows={LOCATIONS.map((l) => [
          `${areaName(l)} (${l})`,
          typeof tick?.charge_level?.[l] === 'number' ? fmtPercent(tick.charge_level[l] as number, 1) : '–',
        ])}
      />
    </>
  );
}

export function PowerCard() {
  const windowKey = useView((v) => v.window);
  return (
    <Panel
      title="Power: asked for vs delivered"
      what="The two lines should overlap. When a controller crashes, watch the gap close within a few seconds."
      how={
        <p>
          The dashed line is the power the fleet was asked for, the solid line is what the batteries actually
          delivered, in megawatts (1 MW = 1,000 kW, about what 670 homes use on average). Above zero the fleet sells
          to the grid; below zero it charges. A red dashed line marks a controller crash: for a moment its batteries
          miss their orders, then the other controllers take them over.
        </p>
      }
    >
      <Readouts />
      <ul className="legend" aria-label="Chart key">
        <li>
          <span className="swatch swatch-line dashed" aria-hidden="true" />
          Asked for (plan)
        </li>
        <li>
          <span className="swatch swatch-line delivered" aria-hidden="true" />
          Delivered
        </li>
        <li>
          <span className="swatch swatch-line crash" aria-hidden="true" />✕ Controller crashed
        </li>
      </ul>
      <PowerChart windowKey={windowKey} />
      <ChargeMeter />
    </Panel>
  );
}
