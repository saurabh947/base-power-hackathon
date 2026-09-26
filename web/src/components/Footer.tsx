import { useState } from 'react';
import { API_BASE } from '../api/config.ts';
import { useBackend } from '../state/backend.ts';
import { leavePresenter, useIsPresenter } from '../state/presenter.ts';
import { showToast } from '../state/toasts.ts';
import { PresenterDialog } from './PresenterDialog.tsx';
import { TechOnly } from './Tech.tsx';

function leave() {
  leavePresenter();
  showToast({ kind: 'info', title: 'Presenter mode is off.' });
}

export function Footer() {
  const presenter = useIsPresenter();
  const { health } = useBackend();
  const [open, setOpen] = useState(false);

  return (
    <footer className="site-footer">
      <div className="site-footer-inner">
        <span>
          Prices are real data from ERCOT, the Texas grid operator. The batteries and the money are simulated.
        </span>
        <span className="footer-spacer" />
        <TechOnly>
          <span className="tech">
            API {API_BASE ?? 'not set'}
            {health ? ` · backend ${health.version}` : ''}
          </span>
        </TechOnly>
        {presenter ? (
          <span>
            Presenter mode on ·{' '}
            <button type="button" className="linklike" onClick={leave}>
              Leave presenter mode
            </button>
          </span>
        ) : (
          <button type="button" className="linklike muted" onClick={() => setOpen(true)}>
            Presenter
          </button>
        )}
      </div>
      <PresenterDialog open={open} onClose={() => setOpen(false)} />
    </footer>
  );
}
