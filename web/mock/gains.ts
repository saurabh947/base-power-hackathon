// GET /results/ai-gains for the practice (mock) server. Dev and tests only; never part of a production build.
//
// 2025 is the REAL measured numbers, copied from RUNLOG.md: "What the AI adds, measured" (2026-09-26,
// `python -m src.backtest.insights`) and the Texas Triangle full-year results (20,000 batteries, lag 3,
// gross wholesale). The mock serves them as the backend would; it does not compute them. The count of 2025
// spikes (21) is the one in docs/API.md's example.
//
// 2026 is SIMULATED practice data (no 2026 backtest exists): plausible numbers a little above 2025's, split
// at real_until_utc (Sep 20) into a "real" part and a part projected on the AI's predicted prices, as
// docs/API.md's Results describe. Nothing about it is a result.

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

/** A per-battery figure's real part (up to real_until_utc) and its projected part (on the AI's prediction). */
export interface RealProjected {
  real: number;
  projected: number;
}

const LABELS: Record<Id, string> = { bands: 'Simple rule', forecast: 'AI forecast (TimesFM-3)', peak: 'Evening peak' };

/** SIMULATED 2026 (see the top): each figure's real and projected parts; the per-battery figure is their sum. */
export const SPLIT_2026: Record<Id | 'best', RealProjected> = {
  bands: { real: 97.9, projected: 33.3 },
  forecast: { real: 151.6, projected: 53.8 },
  peak: { real: 107.3, projected: 35.6 },
  best: { real: 331.0, projected: 117.6 },
};

const total = (p: RealProjected) => round(p.real + p.projected, 2);

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
    year: 2026,
    isPitch: false,
    bestPerBattery: total(SPLIT_2026.best),
    spikeEvents: 18,
    strategies: {
      bands: { perBattery: total(SPLIT_2026.bands), avgSell: 79.1, avgBuy: 17.8, soldMwh: 44_010, surplusMwh: 1_310, spikeMwh: 24.6, spikeCharge: 0.25 },
      forecast: { perBattery: total(SPLIT_2026.forecast), avgSell: 68.2, avgBuy: 17.4, soldMwh: 82_950, surplusMwh: 7_480, spikeMwh: 22.8, spikeCharge: 0.24 },
      peak: { perBattery: total(SPLIT_2026.peak), avgSell: 57.4, avgBuy: 21.2, soldMwh: 85_100, surplusMwh: 5_640, spikeMwh: 33.1, spikeCharge: 0.36 },
    },
  },
];

function round(n: number, decimals = 0): number {
  return Math.round(n * 10 ** decimals) / 10 ** decimals;
}

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

/**
 * docs/API.md AiGains: the AI forecast against the simple rule. With `year` (GET /results/ai-gains?year=), that
 * year's entry; without, the pitch year (2025).
 */
export function mockAiGains(fleet: number, year: number | null = null): AiGains {
  const wanted = MEASURED.filter((y) => (year === null ? y.isPitch : y.year === year));
  const years: AiGainsYear[] = wanted.map((y) => {
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
