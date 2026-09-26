// A faint Texas behind the Start page's content: the live map's red outline
// (the same outline module and projection), with the four city clusters softly
// glowing green. Decorative only: hidden from assistive technology. It draws
// itself in once; with prefers-reduced-motion it is simply there.

import { useMemo } from 'react';
import { useCities } from '../../lib/cities.ts';
import { OUTLINE_FRAME, outlinePathData } from '../../map/outlinePath.ts';
import { backdropCities } from './texasBackdrop.ts';

/** When the city clusters start to glow, after the outline has mostly drawn itself (seconds). */
const CITY_DELAY_S = 1.1;
const CITY_STAGGER_S = 0.18;

export function TexasBackdrop() {
  const cities = useCities();
  const clusters = useMemo(() => backdropCities(cities), [cities]);
  const d = outlinePathData();
  return (
    <div className="texas-backdrop" aria-hidden="true">
      <svg viewBox={`0 0 ${OUTLINE_FRAME.width} ${OUTLINE_FRAME.height}`} preserveAspectRatio="xMidYMid meet" focusable="false">
        <defs>
          <radialGradient id="tx-city-glow">
            <stop offset="0" stopColor="rgb(96, 255, 150)" stopOpacity="0.26" />
            <stop offset="0.45" stopColor="rgb(96, 255, 150)" stopOpacity="0.08" />
            <stop offset="1" stopColor="rgb(96, 255, 150)" stopOpacity="0" />
          </radialGradient>
          <filter id="tx-blur" x="-5%" y="-5%" width="110%" height="110%">
            <feGaussianBlur stdDeviation="6" />
          </filter>
        </defs>
        <path className="tx-glow" d={d} filter="url(#tx-blur)" />
        <path className="tx-line" d={d} pathLength={1} />
        {clusters.map((c, i) => (
          <g key={c.location} className="tx-city" style={{ animationDelay: `${CITY_DELAY_S + i * CITY_STAGGER_S}s` }}>
            <circle cx={c.x} cy={c.y} r={c.glow} fill="url(#tx-city-glow)" />
            {c.dots.map((dot) => (
              <circle key={`${dot.x},${dot.y}`} className="tx-dot" cx={dot.x} cy={dot.y} r={dot.r} opacity={dot.opacity} />
            ))}
          </g>
        ))}
      </svg>
    </div>
  );
}
