// Names and ids for the fleet, per docs/API.md:
// - worker_id is 0–19; people always see "Controller N" with N = worker_id + 1.
// - battery ids are fleet-wide b00000–b19999; areas take consecutive blocks in
//   config order (src/sim/fleet.py: location_counts + assign_locations).

export const DEFAULT_FLEET_SIZE = 20_000;
export const DEFAULT_SHARES: Record<string, number> = { LZ_HOUSTON: 0.5, LZ_NORTH: 0.5 };

/** "Controller 8" for worker_id 7. */
export function controllerName(workerId: number): string {
  return `Controller ${workerId + 1}`;
}

/** The controller number people see, from an event's detail (prefers `controller`, else worker_id + 1). */
export function controllerFromDetail(detail: Record<string, unknown> | undefined): number | null {
  if (!detail) return null;
  if (typeof detail.controller === 'number') return detail.controller;
  if (typeof detail.worker_id === 'number') return detail.worker_id + 1;
  return null;
}

/** "b00123" from 123. */
export function batteryId(index: number): string {
  return `b${String(index).padStart(5, '0')}`;
}

/** Accepts "123", "00123", "b123" or "b00123"; null unless it names a battery in the fleet. */
export function parseBatteryInput(input: string, fleetSize = DEFAULT_FLEET_SIZE): string | null {
  const m = /^\s*b?\s*(\d{1,6})\s*$/i.exec(input);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isInteger(n) && n >= 0 && n < fleetSize ? batteryId(n) : null;
}

export function batteryIndex(id: string): number | null {
  const m = /^b(\d{5})$/.exec(id);
  return m ? Number(m[1]) : null;
}

export interface AreaRange {
  location: string;
  /** First and last battery index, inclusive. */
  first: number;
  last: number;
}

/**
 * Which batteries each area holds: floor(size × share) each, the remainder going to the largest
 * fractions (ties in config order), then consecutive blocks in config order. Mirrors
 * src/sim/fleet.location_counts and assign_locations.
 */
export function batteryRanges(fleetSize: number, shares: Record<string, number>): AreaRange[] {
  const locations = Object.keys(shares);
  const exact = new Map(locations.map((loc) => [loc, fleetSize * shares[loc]]));
  const counts = new Map(locations.map((loc) => [loc, Math.floor(exact.get(loc) ?? 0)]));
  const assigned = [...counts.values()].reduce((a, b) => a + b, 0);
  const byFraction = locations.toSorted(
    (a, b) => (exact.get(b) ?? 0) - (counts.get(b) ?? 0) - ((exact.get(a) ?? 0) - (counts.get(a) ?? 0)),
  );
  for (const loc of byFraction.slice(0, Math.max(0, fleetSize - assigned))) counts.set(loc, (counts.get(loc) ?? 0) + 1);
  let first = 0;
  return locations.map((location) => {
    const count = counts.get(location) ?? 0;
    const range = { location, first, last: first + count - 1 };
    first += count;
    return range;
  });
}

export function areaOfBattery(index: number, ranges: AreaRange[]): string | null {
  return ranges.find((r) => index >= r.first && index <= r.last)?.location ?? null;
}
