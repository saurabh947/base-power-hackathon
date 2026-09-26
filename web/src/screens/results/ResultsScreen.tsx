// Full-year results for one year (the human's "option A"): the year of the run that exists now, else the one
// picked on Start, else 2025 (src/state/resultsYear.ts). GET /results/pitch?year= and GET /results/ai-gains?year=;
// a year with an AI-predicted part (2026) shows its real and projected parts everywhere; a year without results
// (404) says so and offers the re-run, which runs the year shown.

import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client.ts';
import { friendlyError, type FriendlyError } from '../../api/errors.ts';
import type { Results } from '../../api/types.ts';
import { ErrorNotice, Notice } from '../../components/Notice.tsx';
import { Term } from '../../components/Tech.tsx';
import { fmtLocalDate } from '../../lib/format.ts';
import { checkBackend, useBackend } from '../../state/backend.ts';
import { useResultsYear } from '../../state/resultsYear.ts';
import { AiGainsSection } from './AiGainsSection.tsx';
import { readAiGains, type PitchGains, type SplitInput } from './aiGains.ts';
import { headlineOf, splitOf, yearSplit, type YearSplit } from './model.ts';
import { BigNumber, ComparisonBars, HowWeGotThis, TopDays } from './ResultPanels.tsx';
import { RerunPanel } from './RerunPanel.tsx';

function isResults(v: unknown): v is Results {
  return !!v && typeof v === 'object' && Array.isArray((v as Results).strategies);
}

/** "No full-year results for 2026 yet.", with what to do about it. */
function noResults(year: number, technical: string): FriendlyError {
  return {
    title: `No full-year results for ${year} yet.`,
    detail: 'Press "Re-run the full year" below to calculate them.',
    retryAfterS: null,
    technical,
    unreachable: false,
  };
}

interface Loaded {
  results: Results | null;
  error: FriendlyError | null;
  loading: boolean;
}

/** How long the page waits for GET /results/ai-gains before showing the results without that section. */
const GAINS_PATIENCE_MS = 1500;

interface Gains {
  data: PitchGains | null;
  /** The first answer (or failure) is in. */
  settled: boolean;
}

/** For "What the AI adds" in a split year: the AI's and the baseline's real and projected parts. */
function splitInput(results: Results, span: YearSplit | null, gains: PitchGains): SplitInput | null {
  if (!span) return null;
  const ai = results.strategies.find((s) => s.strategy === gains.ai);
  const base = results.strategies.find((s) => s.strategy === gains.baseline);
  const a = ai ? splitOf(ai) : null;
  const b = base ? splitOf(base) : null;
  return a && b ? { span, ai: a, base: b } : null;
}

/** One year's results (keyed by the year, so nothing of another year ever shows while this one loads). */
function YearResults({ year, onResults }: { year: number; onResults: (results: Results | null) => void }) {
  const backend = useBackend();
  const [state, setState] = useState<Loaded>({ results: null, error: null, loading: true });
  // "What the AI adds": shown only when GET /results/ai-gains has something for this year.
  const [gains, setGains] = useState<Gains>({ data: null, settled: false });
  const [patienceOver, setPatienceOver] = useState(false);

  /** GET /results/pitch?year=; state changes only once the answer arrives. */
  const fetchResults = useCallback(
    (signal?: AbortSignal) => {
      api.results(year, signal).then(
        (data) =>
          setState(
            isResults(data) && data.strategies.length
              ? { results: data, error: null, loading: false }
              : { results: null, error: noResults(year, `GET /results/pitch?year=${year} returned no strategies`), loading: false },
          ),
        (err) => {
          if (signal?.aborted) return;
          const f = friendlyError(err, 'results');
          const missing = err instanceof ApiError && err.status === 404;
          // A network blip keeps what is on screen; a real answer replaces it.
          setState((s) => ({ results: f.unreachable ? s.results : null, error: missing ? noResults(year, f.technical) : f, loading: false }));
        },
      );
    },
    [year],
  );

  /**
   * GET /results/ai-gains?year=, alongside the results. A 404 (no_results) or an answer with nothing usable for
   * the year hides the section; a network blip keeps what is already on screen.
   */
  const fetchGains = useCallback(
    (signal?: AbortSignal) => {
      api.aiGains(year, signal).then(
        (raw) => setGains({ data: readAiGains(raw, year), settled: true }),
        (err) => {
          if (signal?.aborted) return;
          const answered = err instanceof ApiError && err.status >= 400 && err.status < 500;
          setGains((g) => ({ data: answered ? null : g.data, settled: true }));
        },
      );
    },
    [year],
  );

  useEffect(() => {
    const controller = new AbortController();
    fetchResults(controller.signal);
    fetchGains(controller.signal);
    // The section sits right under the headline row: wait a moment for it, so it does not push the page down
    // just after it appears, but never hold the results back for long.
    const patience = setTimeout(() => setPatienceOver(true), GAINS_PATIENCE_MS);
    return () => {
      controller.abort();
      clearTimeout(patience);
    };
  }, [fetchResults, fetchGains]);

  const reload = useCallback(() => {
    setState((s) => ({ ...s, loading: true }));
    fetchResults();
    fetchGains();
  }, [fetchResults, fetchGains]);

  const { results, error, loading } = state;
  useEffect(() => onResults(results), [results, onResults]);
  const headline = headlineOf(results);
  const span = results ? yearSplit(results) : null;
  const waitingForGains = !!results && !gains.settled && !patienceOver;

  return (
    <>
      {(loading && !results) || waitingForGains ? (
        <Notice
          kind="waiting"
          title={
            backend.phase === 'waking' ? 'Waking up the simulator… (usually a few seconds)' : 'Loading the full-year results…'
          }
        />
      ) : null}

      {error && !results ? (
        <ErrorNotice
          kind={error.unreachable ? 'error' : 'info'}
          error={error}
          actions={
            error.unreachable ? (
              <button type="button" className="btn btn-sm" onClick={reload}>
                Try again
              </button>
            ) : undefined
          }
        />
      ) : null}

      {waitingForGains ? null : results && headline ? (
        <div className="results-grid">
          <BigNumber results={results} headline={headline} split={span} />
          <ComparisonBars results={results} headline={headline} split={span} />
          {gains.data ? <AiGainsSection gains={gains.data} fleetSize={results.fleet_size} split={splitInput(results, span, gains.data)} /> : null}
          <TopDays results={results} headline={headline} split={span} />
          <div className="span-5 stack">
            <HowWeGotThis results={results} split={span} />
            <RerunPanel year={year} onDone={reload} />
          </div>
        </div>
      ) : !loading ? (
        <RerunPanel year={year} onDone={reload} hasResults={false} />
      ) : null}
    </>
  );
}

/** The subtitle: which prices the year replays, once its results say so. */
function lede(year: number, results: Results | null): string {
  const span = results ? yearSplit(results) : null;
  if (span) {
    return `What the same 20,000 batteries would earn over all of ${year}: real prices from ERCOT, the Texas grid operator, through ${fmtLocalDate(span.lastRealDay)}, then the AI's predicted prices for the rest of the year, so part of it is projected.`;
  }
  if (results) return `What the same 20,000 batteries would have earned over all of ${year}, replaying every real price from ERCOT, the Texas grid operator.`;
  return `What the same 20,000 batteries would earn over all of ${year}.`;
}

export function ResultsScreen() {
  const { year, source } = useResultsYear();
  const [results, setResults] = useState<Results | null>(null);
  const shown = results && results.year === year ? results : null;

  // Is a run going, and which year is it? Ask again on arrival.
  useEffect(() => {
    void checkBackend();
  }, []);

  useEffect(() => {
    if (year !== null) document.title = `Full-year results ${year} · Fleet Sim`;
  }, [year]);

  return (
    <div className="page results">
      <div className="results-head">
        <h1>
          <Term plain="Full-year results" tech="backtest" />
          {year !== null ? <span className="results-year">, {year}</span> : null}
        </h1>
        {year !== null ? (
          <p className="lede">
            {lede(year, shown)}{' '}
            <span className="results-why">
              {source === 'run'
                ? `${year} is the year of the replay running now.`
                : source === 'start'
                  ? `${year} is the year picked on Start.`
                  : 'Pick another year on Start to see its results.'}
            </span>
          </p>
        ) : null}
      </div>

      {year === null ? <Notice kind="waiting" title="Loading the full-year results…" /> : <YearResults key={year} year={year} onResults={setResults} />}
    </div>
  );
}
