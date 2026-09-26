// Helpers for the global "Show technical details" switch (docs/UX.md principle 4).

import type { ReactNode } from 'react';
import { useTechnical } from '../state/prefs.ts';

/** Children appear only while technical details are on. */
export function TechOnly({ children }: { children: ReactNode }) {
  return useTechnical() ? <>{children}</> : null;
}

/** The plain word, plus the spec term in brackets when technical details are on: "Controllers (worker processes)". */
export function Term({ plain, tech }: { plain: ReactNode; tech: string }) {
  const technical = useTechnical();
  return (
    <>
      {plain}
      {technical ? <span className="tech"> ({tech})</span> : null}
    </>
  );
}

/** A dashed box of raw values, shown only while technical details are on. */
export function TechBlock({ rows, children }: { rows?: [string, ReactNode][]; children?: ReactNode }) {
  const technical = useTechnical();
  if (!technical) return null;
  return (
    <div className="tech-block">
      {rows && rows.length ? (
        <dl>
          {rows.map(([k, v]) => (
            <div key={k} style={{ display: 'contents' }}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {children}
    </div>
  );
}
