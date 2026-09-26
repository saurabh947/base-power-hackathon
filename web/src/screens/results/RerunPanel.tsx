import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client.ts';
import { friendlyError, type FriendlyError } from '../../api/errors.ts';
import { Icon } from '../../components/Icon.tsx';
import { ErrorNotice, Notice } from '../../components/Notice.tsx';
import { Panel } from '../../components/Panel.tsx';
import { TechOnly } from '../../components/Tech.tsx';
import { fmtDuration, strategyName } from '../../lib/format.ts';
import { readStorage, writeStorage } from '../../lib/storage.ts';
import { rerunButtons, rerunPhase, stoppedMessage, stopRerun } from './rerun.ts';

const JOB_KEY = 'fleet-sim.backtest-job';
const POLL_MS = 2000;

interface Job {
  id: string;
  startedAtMs: number;
  /** The year it re-runs (the year the page showed when it started). */
  year?: number;
}

/** A run in progress survives navigating away and back (sessionStorage, this tab only). */
function loadJob(): Job | null {
  try {
    const raw = readStorage('session', JOB_KEY);
    const job = raw ? (JSON.parse(raw) as Job) : null;
    return job && typeof job.id === 'string' && typeof job.startedAtMs === 'number' ? job : null;
  } catch {
    return null;
  }
}

function saveJob(job: Job | null): void {
  writeStorage('session', JOB_KEY, job ? JSON.stringify(job) : null);
}

/**
 * "Re-run the full year": POST /backtest for the year shown, then poll GET /backtest/{job_id} until it is done.
 * "Stop" (enabled only while a re-run runs) sends POST /backtest/{job_id}/cancel; the previous results stay.
 * `hasResults` says whether there are previous results to keep showing.
 */
export function RerunPanel({ year, onDone, hasResults = true }: { year: number; onDone: () => void; hasResults?: boolean }) {
  const [job, setJob] = useState<Job | null>(loadJob);
  const [error, setError] = useState<FriendlyError | null>(null);
  const [finished, setFinished] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [skipped, setSkipped] = useState<[string, string][]>([]);
  const [now, setNow] = useState(() => Date.now());
  const buttons = rerunButtons(rerunPhase(job, stopping));

  const start = async () => {
    setError(null);
    setFinished(false);
    setStopped(false);
    setSkipped([]);
    try {
      const { job_id } = await api.backtest(['bands', 'forecast', 'peak'], year);
      const next = { id: job_id, startedAtMs: Date.now(), year };
      saveJob(next);
      setNow(next.startedAtMs);
      setJob(next);
    } catch (err) {
      setError(friendlyError(err, 'backtest'));
    }
  };

  const stop = async () => {
    if (!job || stopping) return;
    setStopping(true);
    setError(null);
    const outcome = await stopRerun(job.id, api.cancelBacktest);
    setStopping(false);
    if (outcome.kind === 'stopped' || outcome.kind === 'unknown') {
      saveJob(null);
      setJob(null);
      setStopped(outcome.kind === 'stopped');
    } else if (outcome.kind === 'error') {
      setError(outcome.error);
    }
    // not_running: it had just finished; the next poll shows how (done, failed or stopped).
  };

  useEffect(() => {
    if (!job) return;
    let cancelled = false;
    const end = (nextError: FriendlyError | null) => {
      saveJob(null);
      setJob(null);
      setError(nextError);
    };
    const check = async () => {
      try {
        const status = await api.backtestStatus(job.id);
        if (cancelled) return;
        if (status.status === 'done') {
          end(null);
          setFinished(true);
          setSkipped(Object.entries(status.skipped ?? {}).filter(([, why]) => typeof why === 'string'));
          onDone();
        } else if (status.status === 'cancelled') {
          // Stopped here, or by someone else: everyone shares the fleet and its re-run.
          end(null);
          setStopped(true);
        } else if (status.status === 'failed') {
          end({
            title: 'The full-year run failed.',
            detail: status.error ?? 'Please try again in a moment.',
            retryAfterS: null,
            technical: `job ${job.id} failed: ${status.error ?? 'no error message'}`,
            unreachable: false,
          });
        }
      } catch (err) {
        if (cancelled) return;
        // A job id the server no longer knows (it restarted) is simply forgotten.
        if (err instanceof ApiError && err.status === 404) {
          end(null);
          return;
        }
        const f = friendlyError(err, 'backtest');
        // Keep polling through blips; give up on real answers.
        if (!f.unreachable) end(f);
      }
    };
    void check();
    const pollTimer = setInterval(() => void check(), POLL_MS);
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      cancelled = true;
      clearInterval(pollTimer);
      clearInterval(clock);
    };
  }, [job, onDone]);

  const elapsed = job ? Math.max(0, (now - job.startedAtMs) / 1000) : 0;

  return (
    <Panel
      title="Re-run the full year"
      what={`Replays every 5 minutes of ${year} for each decision method, with the same code as the live fleet.`}
    >
      <div className="rerun">
        <div className="rerun-actions">
          <button type="button" className="btn" onClick={() => void start()} disabled={!buttons.run}>
            <Icon name="reset" />
            {job ? 'Running…' : 'Re-run the full year'}
          </button>
          <button type="button" className="btn" onClick={() => void stop()} disabled={!buttons.stop}>
            <Icon name="stop" />
            {stopping ? 'Stopping…' : 'Stop'}
          </button>
        </div>
        {job ? (
          <>
            <div
              className="progress"
              role="progressbar"
              aria-label="Full-year run in progress"
              aria-valuetext={`Running for ${fmtDuration(elapsed)}`}
            >
              <div className="progress-bar" />
            </div>
            <p className="muted small">
              Re-running {job.year ?? year} for {fmtDuration(elapsed)}. It usually takes under a minute; you can leave this
              page and come back.
            </p>
            <TechOnly>
              <span className="tech">
                job {job.id} · polling every {POLL_MS / 1000} s
              </span>
            </TechOnly>
          </>
        ) : null}
        {stopped ? <Notice kind="info" title={stoppedMessage(hasResults)} /> : null}
        {finished ? (
          <Notice kind="info" title="Done. The numbers on this page are up to date.">
            {skipped.length
              ? skipped.map(([strategy, why]) => (
                  <div key={strategy}>
                    {strategyName(strategy)} was left out: {why}
                  </div>
                ))
              : null}
          </Notice>
        ) : null}
        {error ? <ErrorNotice error={error} /> : null}
      </div>
    </Panel>
  );
}
