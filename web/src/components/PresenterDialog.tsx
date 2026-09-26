import { useState, type FormEvent } from 'react';
import { api } from '../api/client.ts';
import { friendlyError, type FriendlyError } from '../api/errors.ts';
import { unlockPresenter } from '../state/presenter.ts';
import { showToast } from '../state/toasts.ts';
import { Dialog } from './Dialog.tsx';
import { ErrorNotice } from './Notice.tsx';

/** Asks for the presenter passcode and checks it with POST /presenter/check. */
export function PresenterDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<FriendlyError | null>(null);

  const close = () => {
    setCode('');
    setError(null);
    onClose();
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const passcode = code.trim();
    if (!passcode || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.presenterCheck(passcode);
      unlockPresenter(passcode);
      showToast({
        kind: 'success',
        title: 'Presenter mode is on.',
        detail: '"Crash a controller" is now in the top bar of the Live screen.',
      });
      close();
    } catch (err) {
      setError(friendlyError(err, 'presenter'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={close} title="Presenter mode" initialFocusSelector="#presenter-passcode">
      <form className="dialog-form" onSubmit={submit} style={{ display: 'grid', gap: 14 }}>
        <p className="muted small">
          Presenter mode adds a "Crash a controller" button to the Live screen, for demos. The passcode is kept only in
          this browser tab and is forgotten when you close it.
        </p>
        <div className="field">
          <label className="field-label" htmlFor="presenter-passcode">
            Passcode
          </label>
          <input
            id="presenter-passcode"
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={code}
            onChange={(e) => setCode(e.target.value)}
            required
          />
        </div>
        {error ? <ErrorNotice error={error} /> : null}
        <div className="editor-actions">
          <button type="submit" className="btn btn-primary" disabled={busy || !code.trim()}>
            {busy ? 'Checking…' : 'Unlock presenter mode'}
          </button>
          <button type="button" className="btn btn-quiet" onClick={close}>
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}
