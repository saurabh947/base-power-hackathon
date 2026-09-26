// The header's presenter link (top right, left of "Show technical details"): "Presenter mode" opens the
// passcode dialog; while presenter mode is on it reads "Leave presenter mode" and switches it off. The
// passcode lives in sessionStorage only (src/state/presenter.ts).

import { useState } from 'react';
import { leavePresenter, useIsPresenter } from '../state/presenter.ts';
import { showToast } from '../state/toasts.ts';
import { PresenterDialog } from './PresenterDialog.tsx';

export function PresenterLink() {
  const presenter = useIsPresenter();
  const [open, setOpen] = useState(false);

  if (presenter) {
    return (
      <button
        type="button"
        className="linklike header-presenter"
        onClick={() => {
          leavePresenter();
          showToast({ kind: 'info', title: 'Presenter mode is off.' });
        }}
      >
        Leave presenter mode
      </button>
    );
  }
  return (
    <>
      <button type="button" className="linklike muted header-presenter" onClick={() => setOpen(true)}>
        Presenter mode
      </button>
      <PresenterDialog open={open} onClose={() => setOpen(false)} />
    </>
  );
}
