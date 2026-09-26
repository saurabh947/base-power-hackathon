// Where each city's label goes: beside its cluster of dots, on the side that
// suits the geography (north of Dallas–Fort Worth, east of Houston, west of
// Austin, south-west of San Antonio), moved to another side when that would
// cover a corner panel, another label or another city's dots.

export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export type Side = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';

const ALL_SIDES: Side[] = ['e', 'w', 'n', 's', 'ne', 'nw', 'se', 'sw'];

/** Preferred sides per location id; anything else tries east first. */
export const PREFERRED_SIDES: Record<string, Side[]> = {
  LZ_NORTH: ['n', 'nw', 'ne', 'w', 'e'],
  LZ_HOUSTON: ['e', 'se', 'ne', 's', 'n'],
  LZ_AEN: ['w', 'nw', 'n', 'sw'],
  LZ_CPS: ['sw', 'w', 's', 'nw'],
};

export function place(box: Rect, w: number, h: number, side: Side, gap: number): Rect {
  const cx = (box.left + box.right) / 2;
  const cy = (box.top + box.bottom) / 2;
  let left: number;
  let top: number;
  switch (side) {
    case 'n':
      left = cx - w / 2;
      top = box.top - gap - h;
      break;
    case 's':
      left = cx - w / 2;
      top = box.bottom + gap;
      break;
    case 'e':
      left = box.right + gap;
      top = cy - h / 2;
      break;
    case 'w':
      left = box.left - gap - w;
      top = cy - h / 2;
      break;
    case 'ne':
      left = box.right - gap;
      top = box.top - h + gap;
      break;
    case 'nw':
      left = box.left - w + gap;
      top = box.top - h + gap;
      break;
    case 'se':
      left = box.right - gap;
      top = box.bottom - gap;
      break;
    case 'sw':
      left = box.left - w + gap;
      top = box.bottom - gap;
      break;
  }
  return { left, top, right: left + w, bottom: top + h };
}

function overlap(a: Rect, b: Rect): number {
  const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  return w > 0 && h > 0 ? w * h : 0;
}

function outside(r: Rect, bounds: Rect): number {
  const w = r.right - r.left;
  const h = r.bottom - r.top;
  return w * h - overlap(r, bounds);
}

export interface LabelItem {
  key: string;
  /** The city's dots. */
  box: Rect;
  width: number;
  height: number;
  prefer: Side[];
}

/**
 * A spot for every label, in the order given (put the biggest city first). The first preferred
 * side that is inside `bounds` and clear of `obstacles`, placed labels and other cities' dots
 * wins; failing that, the side that covers the least.
 */
export function placeLabels(items: LabelItem[], obstacles: Rect[], bounds: Rect, gap = 8): Map<string, Rect> {
  const placed = new Map<string, Rect>();
  for (const item of items) {
    const others = items.filter((o) => o.key !== item.key).map((o) => o.box);
    const sides = [...item.prefer, ...ALL_SIDES.filter((s) => !item.prefer.includes(s))];
    let best = place(item.box, item.width, item.height, sides[0], gap);
    let bestCost = Infinity;
    for (let rank = 0; rank < sides.length; rank++) {
      const r = place(item.box, item.width, item.height, sides[rank], gap);
      let cost = outside(r, bounds) * 4;
      for (const o of obstacles) cost += overlap(r, o) * 3;
      for (const o of placed.values()) cost += overlap(r, o) * 3;
      for (const o of others) cost += overlap(r, o);
      // Ties go to the preferred order.
      cost += rank * 0.01;
      if (cost < bestCost) {
        bestCost = cost;
        best = r;
      }
    }
    placed.set(item.key, best);
  }
  return placed;
}
