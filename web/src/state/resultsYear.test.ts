import { describe, expect, it } from 'vitest';
import { DEFAULT_RESULTS_YEAR, pickResultsYear, runYearOf } from './resultsYear.ts';

describe('which year the Results page shows', () => {
  it('shows the year of the run that exists now', () => {
    expect(pickResultsYear({ runExists: true, runYear: 2026, remembered: 2025 })).toBe(2026);
    // The live store may know it before GET /health does.
    expect(pickResultsYear({ runExists: null, runYear: 2026, remembered: null })).toBe(2026);
  });

  it('waits while a run exists but its year has not arrived, or nobody knows yet whether one does', () => {
    expect(pickResultsYear({ runExists: true, runYear: null, remembered: 2026 })).toBeNull();
    expect(pickResultsYear({ runExists: null, runYear: null, remembered: 2026 })).toBeNull();
  });

  it('with no run, shows the year last picked on Start, else 2025', () => {
    expect(pickResultsYear({ runExists: false, runYear: null, remembered: 2026 })).toBe(2026);
    expect(pickResultsYear({ runExists: false, runYear: null, remembered: null })).toBe(DEFAULT_RESULTS_YEAR);
    expect(DEFAULT_RESULTS_YEAR).toBe(2025);
  });

  it('reads a run’s year from its settings, or from where it starts (an older backend)', () => {
    expect(runYearOf({ year: 2026, start_utc: '2026-09-20T23:00:00Z' })).toBe(2026);
    // 11 pm on Dec 31 in Texas is already Jan 1 in UTC: the year is Texas's.
    expect(runYearOf({ start_utc: '2026-01-01T05:00:00Z' } as never)).toBe(2025);
    expect(runYearOf(null)).toBeNull();
  });
});
