import { useTechnical } from '../state/prefs.ts';
import { dismissToast, useToasts } from '../state/toasts.ts';
import { Icon } from './Icon.tsx';

/** Confirmations and errors after button presses, announced to screen readers. */
export function Toasts() {
  const toasts = useToasts();
  const technical = useTechnical();
  return (
    <>
      <div className="sr-only" role="status" aria-live="polite">
        {toasts
          .filter((t) => t.kind !== 'error')
          .map((t) => `${t.title} ${t.detail ?? ''}`)
          .join(' ')}
      </div>
      <div className="sr-only" role="alert">
        {toasts
          .filter((t) => t.kind === 'error')
          .map((t) => `${t.title} ${t.detail ?? ''}`)
          .join(' ')}
      </div>
      <ul className="toasts" aria-label="Messages">
        {toasts.map((t) => (
          <li key={t.id} className={`toast toast-${t.kind}`}>
            <Icon name={t.kind === 'success' ? 'check' : t.kind === 'error' ? 'alert' : 'info'} />
            <div className="notice-body">
              <div className="notice-title">{t.title}</div>
              {t.detail ? <div>{t.detail}</div> : null}
              {technical && t.technical ? <div className="tech">{t.technical}</div> : null}
            </div>
            <button type="button" className="icon-btn" onClick={() => dismissToast(t.id)} aria-label="Dismiss message">
              <Icon name="x" size={16} />
            </button>
          </li>
        ))}
      </ul>
    </>
  );
}
