import { useEffect, useRef, useState } from 'react';
import { api } from '../../api/client.ts';
import { friendlyError, type FriendlyError } from '../../api/errors.ts';
import { editablePart, FALLBACK_SETTINGS, readSettingsResponse, type SettingsModel } from '../../api/settings.ts';
import type { EditableSettings, Settings } from '../../api/types.ts';
import { Icon } from '../../components/Icon.tsx';
import { Link } from '../../components/Link.tsx';
import { ErrorNotice, Notice } from '../../components/Notice.tsx';
import { fmtCount } from '../../lib/format.ts';
import { beginRun } from '../../live/liveStore.ts';
import { navigate } from '../../lib/router.ts';
import { useRetryAfter } from '../../lib/useCountdown.ts';
import { checkBackend, useBackend } from '../../state/backend.ts';
import { RunSettingsCard } from './RunSettingsCard.tsx';

function draftFrom(d: Settings): EditableSettings {
  return { preset: d.preset, speed_tps: d.speed_tps, strategy: d.strategy, start_charge_level: d.start_charge_level };
}

export function StartScreen() {
  const backend = useBackend();
  const [model, setModel] = useState<SettingsModel>(FALLBACK_SETTINGS);
  const [origin, setOrigin] = useState<'loading' | 'server' | 'fallback'>('loading');
  const [draft, setDraft] = useState<EditableSettings>(() => draftFrom(FALLBACK_SETTINGS.defaults));
  // Once the visitor edits the draft, arriving server defaults must not overwrite it.
  const touched = useRef(false);
  const [editing, setEditing] = useState(false);
  const [startOver, setStartOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<FriendlyError | null>(null);
  const [waitS, setWait] = useRetryAfter();

  // Keep the running/idle status fresh while this screen is open.
  useEffect(() => {
    void checkBackend();
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') void checkBackend();
    }, 10_000);
    return () => clearInterval(timer);
  }, []);

  const ready = backend.phase === 'ready';

  useEffect(() => {
    if (!ready || origin !== 'loading') return;
    const controller = new AbortController();
    api.settings(controller.signal).then(
      (raw) => {
        const next = readSettingsResponse(raw);
        setModel(next);
        setOrigin(next.fallback ? 'fallback' : 'server');
        if (!touched.current) setDraft(draftFrom(next.defaults));
      },
      () => {
        if (controller.signal.aborted) return;
        setModel(FALLBACK_SETTINGS);
        setOrigin('fallback');
      },
    );
    return () => controller.abort();
  }, [ready, origin]);

  const onDraft = (next: EditableSettings) => {
    touched.current = true;
    setDraft(next);
  };

  const status = backend.health?.status;
  const runExists = status === 'running' || status === 'paused' || status === 'starting';
  const showRunning = runExists && !startOver;

  const start = async () => {
    if (busy || waitS > 0) return;
    setBusy(true);
    setError(null);
    try {
      const started = await api.start(editablePart(draft, model.editable));
      if (started && typeof started.run_id === 'string' && started.settings) beginRun(started.run_id, started.settings);
      void checkBackend();
      navigate('/live');
    } catch (err) {
      const friendly = friendlyError(err, 'start');
      setError(friendly);
      setWait(friendly.retryAfterS);
    } finally {
      setBusy(false);
    }
  };

  const fleet = fmtCount(model.defaults.fleet_size);

  let startLabel = runExists ? 'Start over with these settings' : 'Start the simulation';
  if (busy) startLabel = 'Starting…';
  else if (waitS > 0) startLabel = `Try again in ${waitS} s`;

  return (
    <div className="page start">
      <div style={{ display: 'grid', gap: 12 }}>
        <h1>What if {fleet} home batteries traded on the Texas power grid?</h1>
        <p className="lede">
          Replay real 2025 electricity prices and watch a fleet of home batteries buy cheap, sell high, and keep working
          when parts of the system crash.
        </p>
      </div>

      {backend.phase === 'waking' ? (
        <Notice kind="waiting" title="Waking up the simulator… (usually a few seconds)">
          The simulator sleeps when nobody is using it. This page will be ready as soon as it answers.
        </Notice>
      ) : null}
      {backend.phase === 'unreachable' && backend.error ? (
        <ErrorNotice
          error={backend.error}
          actions={
            <button type="button" className="btn btn-sm" onClick={() => void checkBackend()}>
              Try again
            </button>
          }
        />
      ) : null}

      {showRunning ? (
        <section className="panel running-card" aria-label="Simulation status">
          <p className="running-title">
            <span className={`dot ${status === 'paused' ? 'dot-warn' : 'dot-ok dot-pulse'}`} aria-hidden="true" />
            {status === 'paused' ? 'A simulation is running (paused right now).' : 'A simulation is running.'}
          </p>
          <p className="muted">
            Everyone who visits watches the same shared fleet. Join in, or start over with your own settings.
          </p>
          <div className="start-actions">
            <Link to="/live" className="btn btn-primary btn-lg">
              Watch live
              <Icon name="chevron" />
            </Link>
            <button type="button" className="btn" onClick={() => setStartOver(true)}>
              Start over
            </button>
          </div>
        </section>
      ) : (
        <>
          <RunSettingsCard model={model} origin={origin} draft={draft} onDraft={onDraft} editing={editing} onEditing={setEditing} />
          {runExists ? (
            <Notice kind="warn" title="Starting over replaces the simulation that is running now, for everyone watching." />
          ) : null}
          {/* A rate-limit notice is only useful until its countdown ends. */}
          {error && !(error.retryAfterS !== null && waitS === 0) ? <ErrorNotice error={error} /> : null}
          <div className="start-actions">
            <button
              type="button"
              className="btn btn-primary btn-lg"
              onClick={() => void start()}
              disabled={!ready || busy || waitS > 0}
            >
              {startLabel}
            </button>
            {runExists ? (
              <button type="button" className="btn btn-quiet" onClick={() => setStartOver(false)}>
                Cancel
              </button>
            ) : null}
            {!ready && backend.phase !== 'unreachable' && backend.phase !== 'not_configured' ? (
              <span className="muted small">Waiting for the simulator…</span>
            ) : null}
          </div>
        </>
      )}

      <ul className="start-facts" aria-label="About this simulation">
        <li>
          <strong>Real prices</strong>
          Every price comes from ERCOT, the Texas grid operator, for 2025.
        </li>
        <li>
          <strong>Keeps working when parts crash</strong>
          20 controllers share the batteries. If one crashes, the others take over.
        </li>
        <li>
          <strong>Honest money</strong>
          Earnings are gross wholesale value, before retail fees and battery wear.
        </li>
      </ul>
    </div>
  );
}
