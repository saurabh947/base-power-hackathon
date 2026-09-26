// Buttons on the Live screen. Each call shows a friendly toast when it fails
// (429, 501, 503 …) and nudges the live view right away when it succeeds, so
// the button feels instant; the next frame from the server confirms it.

import { api } from '../../api/client.ts';
import { friendlyError, type ErrorContext } from '../../api/errors.ts';
import type { RunStatus } from '../../api/types.ts';
import { liveStore } from '../../live/liveStore.ts';
import { fmtSpeed } from '../../lib/format.ts';
import { checkBackend } from '../../state/backend.ts';
import { showToast } from '../../state/toasts.ts';

export async function runAction<T>(
  call: () => Promise<T>,
  context: ErrorContext,
  onSuccess?: (result: T) => void,
): Promise<T | null> {
  try {
    const result = await call();
    onSuccess?.(result);
    return result;
  } catch (err) {
    const f = friendlyError(err, context);
    showToast({ kind: 'error', title: f.title, detail: f.detail, technical: f.technical });
    return null;
  }
}

function patchStatus(status: RunStatus | undefined) {
  if (status) liveStore.set((s) => (s.status === status ? s : { ...s, status }));
}

export const pauseRun = () => runAction(() => api.pause(), 'control', (r) => patchStatus(r?.status ?? 'paused'));

export const resumeRun = () => runAction(() => api.resume(), 'control', (r) => patchStatus(r?.status ?? 'running'));

export const setSpeed = (speedTps: number) =>
  runAction(
    () => api.speed(speedTps),
    'control',
    (r) => {
      const value = typeof r?.speed_tps === 'number' ? r.speed_tps : speedTps;
      liveStore.set((s) => ({ ...s, speedTps: value }));
      showToast({ kind: 'success', title: `Speed: ${fmtSpeed(value)}.` });
    },
  );

export const resetRun = () =>
  runAction(
    () => api.reset(),
    'control',
    () => {
      patchStatus('idle');
      void checkBackend();
      showToast({
        kind: 'success',
        title: 'Simulation reset.',
        detail: 'All 20 controllers restarted and every battery is back online.',
      });
    },
  );
