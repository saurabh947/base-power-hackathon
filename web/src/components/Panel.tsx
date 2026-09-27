// Every panel explains itself (docs/UX.md principle 3): a title, a one-line
// "What is this?" underneath (the Results panels leave it out: their titles and
// figures say it), and an optional "How it works" of at most three sentences
// behind a native <details> disclosure.

import { useId, type ReactNode } from 'react';
import { Icon } from './Icon.tsx';

interface PanelProps {
  title: ReactNode;
  what?: ReactNode;
  how?: ReactNode;
  actions?: ReactNode;
  className?: string;
  headingLevel?: 2 | 3;
  children?: ReactNode;
}

export function Panel({ title, what, how, actions, className, headingLevel = 2, children }: PanelProps) {
  const id = useId();
  const Heading = headingLevel === 2 ? 'h2' : 'h3';
  return (
    <section className={`panel${className ? ` ${className}` : ''}`} aria-labelledby={id}>
      <div className="panel-head">
        <Heading className="panel-title" id={id}>
          {title}
        </Heading>
        {actions ? <div className="panel-actions">{actions}</div> : null}
      </div>
      {what ? (
        <p className="panel-what">
          <span className="sr-only">What is this? </span>
          {what}
        </p>
      ) : null}
      {how ? <HowItWorks>{how}</HowItWorks> : null}
      {children ? <div className="panel-body">{children}</div> : null}
    </section>
  );
}

export function HowItWorks({ children, label = 'How it works' }: { children: ReactNode; label?: string }) {
  return (
    <details className="how">
      <summary>
        <Icon name="chevron" size={16} />
        {label}
      </summary>
      <div className="how-body">{children}</div>
    </details>
  );
}
