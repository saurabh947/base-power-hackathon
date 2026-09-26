import { describe, expect, it } from 'vitest';
import { ApiError } from '../../api/client.ts';
import { rerunButtons, rerunPhase, stoppedMessage, stopRerun } from './rerun.ts';

describe('Re-run and Stop buttons', () => {
  it('enables Stop only while a re-run is running, and the run button only when nothing runs', () => {
    expect(rerunButtons('idle')).toEqual({ run: true, stop: false });
    expect(rerunButtons('running')).toEqual({ run: false, stop: true });
    // A Stop has been sent: neither button until the answer.
    expect(rerunButtons('stopping')).toEqual({ run: false, stop: false });
  });

  it('knows the phase from the job and whether a Stop is on its way', () => {
    const job = { id: 'job-1', startedAtMs: 0 };
    expect(rerunPhase(null, false)).toBe('idle');
    expect(rerunPhase(null, true)).toBe('idle');
    expect(rerunPhase(job, false)).toBe('running');
    expect(rerunPhase(job, true)).toBe('stopping');
  });
});

/** A cancel call that fails with `err`. */
const failWith = (err: unknown) => async () => {
  throw err;
};

describe('stopping a re-run (POST /backtest/{job_id}/cancel)', () => {
  it('sends the job id and reports a stop on 200 {"status": "cancelled"}', async () => {
    const sent: string[] = [];
    const outcome = await stopRerun('job-1', async (id) => {
      sent.push(id);
      return { status: 'cancelled' };
    });
    expect(sent).toEqual(['job-1']);
    expect(outcome).toEqual({ kind: 'stopped' });
  });

  it('turns 409 not_running and 404 into what the panel does next', async () => {
    expect(await stopRerun('job-1', failWith(new ApiError(409, 'not_running', 'That full-year run is not running.', null)))).toEqual({
      kind: 'not_running',
    });
    expect(await stopRerun('job-1', failWith(new ApiError(404, 'unknown_job', null, null)))).toEqual({ kind: 'unknown' });
  });

  it('keeps the re-run going, with a plain error, when the simulator cannot be reached', async () => {
    const outcome = await stopRerun('job-1', failWith(new ApiError(0, 'network', null, null)));
    expect(outcome).toMatchObject({ kind: 'error', error: { unreachable: true, title: "Can't reach the simulator." } });
  });

  it('says the previous results are still shown, or that none were saved', () => {
    expect(stoppedMessage(true)).toBe('Stopped. The previous results are still shown.');
    expect(stoppedMessage(false)).toBe('Stopped. No full-year results were saved.');
  });
});
