interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  describedBy?: string;
}

/** An on/off switch: a button with role="switch" (Space and Enter toggle it). */
export function Switch({ checked, onChange, label, describedBy }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-describedby={describedBy}
      className="switch"
      onClick={() => onChange(!checked)}
    >
      <span className="switch-track" aria-hidden="true" />
      <span>{label}</span>
      <span className="switch-state" aria-hidden="true">
        {checked ? 'On' : 'Off'}
      </span>
    </button>
  );
}
