import { useCallback, useEffect, useState } from 'react';

/**
 * Seconds left until a retry is allowed (for 429 "retry_after_s"). Returns the
 * whole seconds remaining and a setter taking seconds from now (or null).
 */
export function useRetryAfter(): [number, (seconds: number | null) => void] {
  const [until, setUntil] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (until === null) return;
    const timer = setInterval(() => {
      const t = Date.now();
      setNow(t);
      if (t >= until) {
        clearInterval(timer);
        setUntil(null);
      }
    }, 250);
    return () => clearInterval(timer);
  }, [until]);

  const set = useCallback((seconds: number | null) => {
    const t = Date.now();
    setNow(t);
    setUntil(seconds && seconds > 0 ? t + seconds * 1000 : null);
  }, []);

  const left = until === null ? 0 : Math.max(0, Math.ceil((until - now) / 1000));
  return [left, set];
}
