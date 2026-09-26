import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client.ts';
import { friendlyError, type FriendlyError } from '../../api/errors.ts';
import type { Results } from '../../api/types.ts';
import { ErrorNotice, Notice } from '../../components/Notice.tsx';
import { Term } from '../../components/Tech.tsx';
import { useBackend } from '../../state/backend.ts';
import { AiGainsSection } from './AiGainsSection.tsx';
import { readAiGains, type PitchGains } from './aiGains.ts';
import { headlineOf } from './model.ts';
import { BigNumber, ComparisonBars, HowWeGotThis, TopDays } from './ResultPanels.tsx';
import { RerunPanel } from './RerunPanel.tsx';

function isResults(v: unknown): v is Results {
  return !!v && typeof v === 'object' && Array.isArray((v as Results).strategies);
}

const EMPTY: FriendlyError = {
  title: 'No full-year results yet.',
  detail: 'Run the full year to calculate them.',
  retryAfterS: null,
  technical: 'GET /results/pitch returned no strategies',
  unreachable: false,
};

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

export function ResultsScreen() {
  const backend = useBackend();
  const [state, setState] = useState<Loaded>({ results: null, error: null, loading: true });
  // "What the AI adds": shown only when GET /results/ai-gains has something to show.
  const [gains, setGains] = useState<Gains>({ data: null, settled: false });
  const [patienceOver, setPatienceOver] = useState(false);

  /** GET /results/pitch; state changes only once the answer arrives. */
  const fetchResults = useCallback((signal?: AbortSignal) => {
    api.results(signal).then(
      (data) =>
        setState(
          isResults(data) && data.strategies.length
            ? { results: data, error: null, loading: false }
            : { results: null, error: EMPTY, loading: false },
        ),
      (err) => {
        if (!signal?.aborted) setState((s) => ({ ...s, error: friendlyError(err, 'results'), loading: false }));
      },
    );
  }, []);

  /**
   * GET /results/ai-gains, alongside the results. A 404 (no_results) or an answer with nothing usable hides
   * the section; a network blip keeps what is already on screen.
   */
  const fetchGains = useCallback((signal?: AbortSignal) => {
    api.aiGains(signal).then(
      (raw) => setGains({ data: readAiGains(raw), settled: true }),
      (err) => {
        if (signal?.aborted) return;
        const answered = err instanceof ApiError && err.status >= 400 && err.status < 500;
        setGains((g) => ({ data: answered ? null : g.data, settled: true }));
      },
    );
  }, []);

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
  const headline = headlineOf(results);
  const year = results?.year ?? 2025;
  const waitingForGains = !!results && !gains.settled && !patienceOver;

  return (
    <div className="page results">
      <div className="results-head">
        <h1>
          <Term plain="Full-year results" tech="backtest" />
        </h1>
        <p className="lede">
          What the same 20,000 batteries would have earned over all of {year}, replaying every real price from ERCOT,
          the Texas grid operator.
        </p>
      </div>

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
          <BigNumber results={results} headline={headline} />
          <ComparisonBars results={results} headline={headline} />
          {gains.data ? <AiGainsSection gains={gains.data} fleetSize={results.fleet_size} /> : null}
          <TopDays results={results} headline={headline} />
          <div className="span-5 stack">
            <HowWeGotThis results={results} />
            <RerunPanel year={year} onDone={reload} />
          </div>
        </div>
      ) : !loading ? (
        <RerunPanel year={year} onDone={reload} hasResults={false} />
      ) : null}
    </div>
  );
}
