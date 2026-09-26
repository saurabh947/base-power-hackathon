import { useCallback, useEffect, useState } from 'react';
import { api } from '../../api/client.ts';
import { friendlyError, type FriendlyError } from '../../api/errors.ts';
import type { Results } from '../../api/types.ts';
import { ErrorNotice, Notice } from '../../components/Notice.tsx';
import { Term } from '../../components/Tech.tsx';
import { useBackend } from '../../state/backend.ts';
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

export function ResultsScreen() {
  const backend = useBackend();
  const [state, setState] = useState<Loaded>({ results: null, error: null, loading: true });

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

  useEffect(() => {
    const controller = new AbortController();
    fetchResults(controller.signal);
    return () => controller.abort();
  }, [fetchResults]);

  const reload = useCallback(() => {
    setState((s) => ({ ...s, loading: true }));
    fetchResults();
  }, [fetchResults]);

  const { results, error, loading } = state;
  const headline = headlineOf(results);
  const year = results?.year ?? 2025;

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

      {loading && !results ? (
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

      {results && headline ? (
        <div className="results-grid">
          <BigNumber results={results} headline={headline} />
          <ComparisonBars results={results} headline={headline} />
          {/* "What the AI adds" (coming later) goes here, as its own row: a full-width panel (className="span-12"),
              or two panels of span-7 and span-5 like the rows around it. */}
          <TopDays results={results} headline={headline} />
          <div className="span-5 stack">
            <HowWeGotThis results={results} />
            <RerunPanel year={year} onDone={reload} />
          </div>
        </div>
      ) : !loading ? (
        <RerunPanel year={year} onDone={reload} />
      ) : null}
    </div>
  );
}
