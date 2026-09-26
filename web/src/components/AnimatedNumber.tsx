import { useLayoutEffect, useRef } from 'react';

const reducedMotion = () =>
  typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

/**
 * A number that glides to each new value, written straight to the DOM so the
 * animation never re-renders React. `format` should be a stable function.
 * Decorative: pair it with a screen-reader text that states the value.
 */
export function AnimatedNumber({
  value,
  format,
  durationMs = 800,
}: {
  value: number;
  format: (n: number) => string;
  durationMs?: number;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const shown = useRef<number | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const from = shown.current;
    if (from === null || from === value || reducedMotion() || !Number.isFinite(from) || !Number.isFinite(value)) {
      shown.current = value;
      el.textContent = format(value);
      return;
    }
    const start = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      const eased = 1 - (1 - t) ** 3;
      shown.current = from + (value - from) * eased;
      el.textContent = format(shown.current);
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [value, format, durationMs]);

  return <span ref={ref} />;
}
