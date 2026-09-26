import { useId } from 'react';

interface Option<V extends string> {
  value: V;
  label: string;
}

interface SegmentedProps<V extends string> {
  label: string;
  value: V;
  options: Option<V>[];
  onChange: (value: V) => void;
  /** Hide the group label visually (it is still read by screen readers). */
  hideLabel?: boolean;
}

/** A radio group styled as a segmented control; arrow keys move between options. */
export function Segmented<V extends string>({ label, value, options, onChange, hideLabel }: SegmentedProps<V>) {
  const name = useId();
  const labelId = useId();
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      <span id={labelId} className={hideLabel ? 'sr-only' : 'toolbar-label'}>
        {label}
      </span>
      <span className="segmented" role="radiogroup" aria-labelledby={labelId}>
        {options.map((o) => (
          <label key={o.value}>
            <input
              type="radio"
              name={name}
              value={o.value}
              checked={value === o.value}
              onChange={() => onChange(o.value)}
            />
            <span>{o.label}</span>
          </label>
        ))}
      </span>
    </span>
  );
}
