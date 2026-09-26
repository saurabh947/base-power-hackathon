import { useEffect, useState } from 'react';
import { api } from '../../api/client.ts';
import { friendlyError, type FriendlyError } from '../../api/errors.ts';
import { Icon } from '../../components/Icon.tsx';
import { ErrorNotice, Notice } from '../../components/Notice.tsx';
import { Panel } from '../../components/Panel.tsx';
import { TechOnly } from '../../components/Tech.tsx';
import { fmtDuration } from '../../lib/format.ts';
import { readStorage, writeStorage } from '../../lib/storage.ts';

const JOB_KEY = 'fleet-sim.backtest-job';
const POLL_MS = 2000;

interface Job {
  id: string;
  startedAtMs: number;
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

/** "Re-run the full year": POST /backtest, then poll GET /backtest/{job_id} until it is done. */
export function RerunPanel({ year, onDone }: { year: number; onDone: () => void }) {
  const [job, setJob] = useState<Job | null>(loadJob);
  const [error, setError] = useState<FriendlyError | null>(null);
  const [finished, setFinished] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  const start = async () => {
    setError(null);
    setFinished(false);
    try {
      const { job_id } = await api.backtest(['bands', 'forecast', 'peak'], year);
      const next = { id: job_id, startedAtMs: Date.now() };
      saveJob(next);
      setNow(next.startedAtMs);
      setJob(next);
    } catch (err) {
      setError(friendlyError(err, 'backtest'));
    }
  };

  useEffect(() => {
    if (!job) return;
    let cancelled = false;
    const stop = (nextError: FriendlyError | null) => {
      saveJob(null);
      setJob(null);
      setError(nextError);
    };
    const check = async () => {
      try {
        const status = await api.backtestStatus(job.id);
        if (cancelled) return;
        if (status.status === 'done') {
          stop(null);
          setFinished(true);
          onDone();
        } else if (status.status === 'failed') {
          stop({
            title: 'The full-year run failed.',
            detail: status.error ?? 'Please try again in a moment.',
            retryAfterS: null,
            technical: `job ${job.id} failed: ${status.error ?? 'no error message'}`,
            unreachable: false,
          });
        }
      } catch (err) {
        if (cancelled) return;
        const f = friendlyError(err, 'backtest');
        // Keep polling through blips; give up on real answers such as 404.
        if (!f.unreachable) stop(f);
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
        <div>
          <button type="button" className="btn" onClick={() => void start()} disabled={!!job}>
            <Icon name="reset" />
            {job ? 'Running…' : 'Re-run the full year'}
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
              Running for {fmtDuration(elapsed)}. It usually takes under a minute; you can leave this page and come
              back.
            </p>
            <TechOnly>
              <span className="tech">
                job {job.id} · polling every {POLL_MS / 1000} s
              </span>
            </TechOnly>
          </>
        ) : null}
        {finished ? <Notice kind="info" title="Done. The numbers on this page are up to date." /> : null}
        {error ? <ErrorNotice error={error} /> : null}
      </div>
    </Panel>
  );
}
