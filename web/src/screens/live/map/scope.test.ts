import { describe, expect, it } from 'vitest';
import type { Availability, Tick, Totals, Worker } from '../../../api/types.ts';
import { scopeAvailability, scopeTotals, scopeTracking, scopeValue, trackingErrorPct, workerShare } from './scope.ts';

const tick = {
  target_kw: { LZ_HOUSTON: 20_000, LZ_NORTH: 30_000, fleet: 50_000 },
  actual_kw: { LZ_HOUSTON: 19_000, LZ_NORTH: 30_000, fleet: 49_000 },
  error_pct: 2,
} as unknown as Tick;

describe('the corners follow the selected city', () => {
  it('reads a city value, or the fleet total (or the sum of the areas)', () => {
    expect(scopeValue(tick.target_kw, 'LZ_NORTH')).toBe(30_000);
    expect(scopeValue(tick.target_kw, null)).toBe(50_000);
    expect(scopeValue({ LZ_HOUSTON: 1, LZ_NORTH: 2 }, null)).toBe(3);
    expect(scopeValue(tick.target_kw, 'LZ_AEN')).toBeNull();
    expect(scopeValue(undefined, null)).toBeNull();
  });

  it('takes money from totals.by_location', () => {
    const totals: Totals = {
      revenue_usd: 100,
      sold_kwh: 10,
      bought_kwh: 5,
      ticks: 3,
      by_location: { LZ_NORTH: { revenue_usd: 60, sold_kwh: 6, bought_kwh: 3 } },
    };
    expect(scopeTotals(totals, null)).toBe(totals);
    expect(scopeTotals(totals, 'LZ_NORTH')).toEqual({ revenue_usd: 60, sold_kwh: 6, bought_kwh: 3 });
    expect(scopeTotals(totals, 'LZ_AEN')).toBeNull();
  });

  it('takes batteries online from availability.by_location', () => {
    const a: Availability = {
      available: 19_000,
      total: 20_000,
      by_location: { LZ_CPS: { available: 2000, total: 2600 } },
      offline_workers: [],
    };
    expect(scopeAvailability(a, null)).toEqual({ available: 19_000, total: 20_000 });
    expect(scopeAvailability(a, 'LZ_CPS')).toEqual({ available: 2000, total: 2600 });
    expect(scopeAvailability(null, 'LZ_CPS')).toBeNull();
  });

  it("counts a controller's batteries in the city from workers[].by_location", () => {
    const w = { worker_id: 3, batteries: 1000, by_location: { LZ_NORTH: 400, LZ_HOUSTON: 350 } } as unknown as Worker;
    expect(workerShare(w, null)).toBe(1000);
    expect(workerShare(w, 'LZ_NORTH')).toBe(400);
    expect(workerShare(w, 'LZ_AEN')).toBe(0);
  });

  it('measures tracking like error_pct: the fleet uses the tick, a city is computed', () => {
    expect(scopeTracking(tick, null, 20_000, 8.25).errorPct).toBe(2);
    expect(scopeTracking(tick, 'LZ_HOUSTON', 7000, 8.25).errorPct).toBeCloseTo(5, 10);
    expect(scopeTracking(null, 'LZ_HOUSTON', 7000, 8.25)).toEqual({ target: null, actual: null, errorPct: null });
    // Near zero, the floor is 0.5% of nameplate: 10 kW off with nothing asked is not "infinitely" off.
    expect(trackingErrorPct(0, 10, 7000 * 8.25)).toBeCloseTo((10 / (0.005 * 57_750)) * 100, 10);
  });
});
