// GET /results/ai-gains for the practice (mock) server. Dev and tests only; never part of a production build.
//
// These are the REAL measured numbers, copied from RUNLOG.md: "What the AI adds, measured" (2026-09-26,
// `python -m src.backtest.insights`) and the Texas Triangle full-year results (20,000 batteries, lag 3,
// gross wholesale). The mock serves them as the backend would; it does not compute them. The count of 2025
// spikes (21) is the one in docs/API.md's example; 2024's (53) is the measured count.

import type { AiGains, AiGainsYear, StrategyGains } from '../src/api/types.ts';

/** Also a marker for scripts/check-dist.mjs ("The practice server"): none of this may ship. */
export const GAINS_SOURCE =
  'The practice server serves the measured numbers from RUNLOG.md (What the AI adds, measured); it does not compute them.';

type Id = 'bands' | 'forecast' | 'peak';

interface Measured {
  /** $ per battery per year. */
  perBattery: number;
  avgSell: number;
  avgBuy: number;
  soldMwh: number;
  /** MWh bought at ≤ $0/MWh. */
  surplusMwh: number;
  /** MWh sold during 5-minute prices ≥ $1,000/MWh. */
  spikeMwh: number;
  /** Charge level when the spikes began, averaged. */
  spikeCharge: number;
}

interface MeasuredYear {
  year: number;
  isPitch: boolean;
  bestPerBattery: number;
  spikeEvents: number;
  strategies: Record<Id, Measured>;
}

const LABELS: Record<Id, string> = { bands: 'Simple rule', forecast: 'AI forecast (TimesFM-3)', peak: 'Evening peak' };

export const MEASURED: readonly MeasuredYear[] = [
  {
    year: 2025,
    isPitch: true,
    bestPerBattery: 421.29,
    spikeEvents: 21,
    strategies: {
      bands: { perBattery: 123.67, avgSell: 77.63, avgBuy: 17.32, soldMwh: 42_332, surplusMwh: 1_239, spikeMwh: 27.9, spikeCharge: 0.24 },
      forecast: { perBattery: 190.95, avgSell: 66.58, avgBuy: 17.02, soldMwh: 80_168.1, surplusMwh: 7_011.3, spikeMwh: 25.2, spikeCharge: 0.231 },
      peak: { perBattery: 136.7, avgSell: 56.03, avgBuy: 20.87, soldMwh: 83_203, surplusMwh: 5_365, spikeMwh: 36.9, spikeCharge: 0.35 },
    },
  },
  {
    year: 2024,
    isPitch: false,
    bestPerBattery: 526.54,
    spikeEvents: 53,
    strategies: {
      bands: { perBattery: 113.71, avgSell: 64.17, avgBuy: 12.69, soldMwh: 45_406, surplusMwh: 905, spikeMwh: 76.3, spikeCharge: 0.24 },
      forecast: { perBattery: 271.45, avgSell: 76.76, avgBuy: 12.13, soldMwh: 85_786, surplusMwh: 6_361, spikeMwh: 610.9, spikeCharge: 0.52 },
      peak: { perBattery: 150.51, avgSell: 55.35, avgBuy: 17.36, soldMwh: 83_430, surplusMwh: 3_552, spikeMwh: 298.4, spikeCharge: 0.64 },
    },
  },
];

const round = (n: number, decimals = 0) => Math.round(n * 10 ** decimals) / 10 ** decimals;

function strategyGains(id: Id, m: Measured, year: MeasuredYear, fleet: number): StrategyGains {
  const fleetUsd = Math.round(m.perBattery * fleet);
  return {
    label: LABELS[id],
    per_battery_usd_year: m.perBattery,
    fleet_usd_year: fleetUsd,
    share_of_best: round(m.perBattery / year.bestPerBattery, 3),
    surplus_mwh: m.surplusMwh,
    spike_mwh: m.spikeMwh,
    spike_events: year.spikeEvents,
    spike_charge_level: m.spikeCharge,
    sold_mwh: m.soldMwh,
    // Not in the RUNLOG table: from revenue = sold × average sell price − bought × average buy price.
    bought_mwh: round((m.soldMwh * m.avgSell - fleetUsd) / m.avgBuy, 1),
    avg_sell_usd_mwh: m.avgSell,
    avg_buy_usd_mwh: m.avgBuy,
  };
}

/** docs/API.md AiGains: the AI forecast against the simple rule, the pitch year first. */
export function mockAiGains(fleet: number): AiGains {
  const years: AiGainsYear[] = MEASURED.map((y) => {
    const strategies = Object.fromEntries(
      (Object.keys(y.strategies) as Id[]).map((id) => [id, strategyGains(id, y.strategies[id], y, fleet)]),
    ) as Record<Id, StrategyGains>;
    const perBattery = round(y.strategies.forecast.perBattery - y.strategies.bands.perBattery, 2);
    return {
      year: y.year,
      is_pitch: y.isPitch,
      best_possible_per_battery_usd_year: y.bestPerBattery,
      strategies,
      uplift: {
        per_battery_usd_year: perBattery,
        pct: round(perBattery / y.strategies.bands.perBattery, 3),
        fleet_usd_year: Math.round(perBattery * fleet),
      },
    };
  });
  return { ai: 'forecast', baseline: 'bands', years };
}
