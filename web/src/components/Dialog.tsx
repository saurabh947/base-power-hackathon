// Native <dialog> opened with showModal(): the page behind becomes inert, focus
// stays inside, and Escape closes it. Focus returns to whatever opened it.

import { useEffect, useId, useRef, type MouseEvent, type ReactNode } from 'react';
import { Icon } from './Icon.tsx';

interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  variant?: 'modal' | 'drawer';
  children: ReactNode;
  /** Element to focus when opened (defaults to the first focusable control). */
  initialFocusSelector?: string;
}

export function Dialog({ open, onClose, title, variant = 'modal', children, initialFocusSelector }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const returnFocus = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      dialog.showModal();
      const target = initialFocusSelector ? dialog.querySelector<HTMLElement>(initialFocusSelector) : null;
      target?.focus();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open, initialFocusSelector]);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    const handleClose = () => {
      onClose();
      const el = returnFocus.current;
      returnFocus.current = null;
      if (el && el.isConnected) el.focus();
    };
    dialog.addEventListener('close', handleClose);
    return () => dialog.removeEventListener('close', handleClose);
  }, [onClose]);

  // A click on the backdrop (the dialog element itself, outside its content) closes it.
  const onDialogClick = (e: MouseEvent<HTMLDialogElement>) => {
    if (e.target === ref.current) ref.current?.close();
  };

  return (
    // oxlint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions -- Escape closes natively; the click only handles the backdrop
    <dialog ref={ref} className={variant} aria-labelledby={titleId} onClick={onDialogClick}>
      {open ? (
        <div className="dialog-inner">
          <div className="dialog-head">
            <h2 id={titleId}>{title}</h2>
            <button type="button" className="icon-btn" onClick={() => ref.current?.close()} aria-label="Close">
              <Icon name="x" />
            </button>
          </div>
          {children}
        </div>
      ) : null}
    </dialog>
  );
}
