import { API_BASE } from '../api/config.ts';
import { friendlyError } from '../api/errors.ts';
import { ApiError } from '../api/client.ts';
import { isPracticeServer, useBackend } from '../state/backend.ts';
import { Icon } from './Icon.tsx';
import { ErrorNotice } from './Notice.tsx';

/** Honest labels: say so when the data comes from the practice (mock) server, or from nowhere. */
export function PracticeBanner() {
  const { health } = useBackend();
  if (!API_BASE) {
    return (
      <div className="page" style={{ paddingTop: 16 }}>
        <ErrorNotice error={friendlyError(new ApiError(0, 'not_configured', null, null))} />
      </div>
    );
  }
  if (!isPracticeServer(health)) return null;
  return (
    <div className="practice-banner" role="note">
      <div className="page">
        <Icon name="info" />
        <span>
          <strong>Practice data.</strong> You are connected to the mock simulator. Real: ERCOT prices for the replay
          days, and the main full-year figures (copied from the real backtest). Everything else is simulated.
        </span>
      </div>
    </div>
  );
}
