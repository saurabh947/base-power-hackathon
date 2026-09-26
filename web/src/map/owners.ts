// Which controller runs which battery, replayed on the client from the events.
// The backend starts each controller on a contiguous block of battery ids
// (FleetLayout.default_owners: numpy.array_split of the fleet into 20 chunks, so
// workers 0-6 are Houston, 7-14 Dallas–Fort Worth, and so on), and when one
// dies it deals that controller's batteries out round-robin, in id order, to the
// healthy controllers listed in worker_adopted.detail.adopted_by
// (coordinator._reassign). The map uses this to flash a crashed controller's
// batteries when a frame missed their "controller down" code.

/** Owner of every battery at the start of a run: `workers` contiguous chunks, the first ones one longer. */
export function defaultOwners(size: number, workers: number): Int16Array {
  const owners = new Int16Array(size);
  const n = Math.max(1, workers);
  const small = Math.floor(size / n);
  const extra = size % n;
  let g = 0;
  for (let w = 0; w < n; w++) {
    const len = small + (w < extra ? 1 : 0);
    owners.fill(w, g, g + len);
    g += len;
  }
  return owners;
}

/** The batteries `worker` runs now, in id order. */
export function cohortOf(owners: Int16Array, worker: number): Int32Array {
  let count = 0;
  for (let g = 0; g < owners.length; g++) if (owners[g] === worker) count++;
  const out = new Int32Array(count);
  for (let g = 0, k = 0; g < owners.length && k < count; g++) if (owners[g] === worker) out[k++] = g;
  return out;
}

/** Hand `from`'s batteries round-robin to `adoptedBy` (battery i of the cohort to adoptedBy[i % n]); returns the cohort. */
export function applyAdoption(owners: Int16Array, from: number, adoptedBy: readonly number[]): Int32Array {
  const cohort = cohortOf(owners, from);
  if (!adoptedBy.length) {
    for (const g of cohort) owners[g] = -1; // orphans until a controller is healthy
    return cohort;
  }
  cohort.forEach((g, i) => {
    owners[g] = adoptedBy[i % adoptedBy.length];
  });
  return cohort;
}
