import type { TickAction } from '../../api/types.ts';
import { Icon } from '../../components/Icon.tsx';
import { actionLabel } from '../../lib/format.ts';

const CLASS: Record<TickAction, string> = { charge: 'chip-charge', sell: 'chip-sell', hold: 'chip-hold' };
const ICON = { charge: 'down', sell: 'up', hold: 'minus' } as const;

/** "▼ Charging", "▲ Selling", "— Waiting": color plus an arrow plus a word. */
export function ActionChip({ action }: { action: TickAction | null | undefined }) {
  if (!action || !(action in CLASS)) return <span className="chip chip-hold">No data yet</span>;
  return (
    <span className={`chip ${CLASS[action]}`}>
      <Icon name={ICON[action]} size={14} />
      {actionLabel(action)}
    </span>
  );
}
