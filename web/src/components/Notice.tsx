import type { ReactNode } from 'react';
import type { FriendlyError } from '../api/errors.ts';
import { useTechnical } from '../state/prefs.ts';
import { Icon, type IconName } from './Icon.tsx';

type Kind = 'info' | 'waiting' | 'warn' | 'error';

const ICONS: Record<Kind, IconName> = { info: 'info', waiting: 'spinner', warn: 'alert', error: 'alert' };

interface NoticeProps {
  kind?: Kind;
  title: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  /** Announce politely (status) or assertively (alert). Errors default to alert. */
  live?: 'polite' | 'assertive' | 'off';
}

export function Notice({ kind = 'info', title, children, actions, live }: NoticeProps) {
  const mode = live ?? (kind === 'error' ? 'assertive' : 'polite');
  const role = mode === 'off' ? undefined : mode === 'assertive' ? 'alert' : 'status';
  return (
    <div className={`notice notice-${kind}`} role={role}>
      <Icon name={ICONS[kind]} className={kind === 'waiting' ? 'spinner' : undefined} />
      <div className="notice-body">
        <div className="notice-title">{title}</div>
        {children ? <div>{children}</div> : null}
        {actions ? <div className="notice-actions">{actions}</div> : null}
      </div>
    </div>
  );
}

/** A FriendlyError, with its raw status and code under "Show technical details". */
export function ErrorNotice({
  error,
  actions,
  kind = 'error',
}: {
  error: FriendlyError;
  actions?: ReactNode;
  kind?: Kind;
}) {
  const technical = useTechnical();
  return (
    <Notice kind={kind} title={error.title} actions={actions}>
      {error.detail}
      {technical ? <div className="tech">{error.technical}</div> : null}
    </Notice>
  );
}
