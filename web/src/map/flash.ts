// The crash flash when a frame missed it. "Controller down" (code 4) lasts
// only about two ticks, so at 5-10 ticks a second a once-a-second frame can
// miss it. The worker events cannot be missed: when worker_suspect, worker_dead
// or worker_adopted arrives and the frame shows no code 4 (and none was shown a
// moment ago), that controller's batteries are shown as down in this frame. The
// next frame shows them handed over, so the map's usual flash and re-light play.
// Which batteries a controller runs is replayed from the events (./owners.ts).

import type { LiveEvent } from '../api/types.ts';
import { applyAdoption, applyRecovery, cohortOf, defaultOwners } from './owners.ts';
import { DOWN, OFFLINE } from './status.ts';

const WORKER_EVENTS = new Set(['worker_suspect', 'worker_dead', 'worker_adopted']);

/** A real code 4 shown this recently means the flash already played for this crash. */
export const RECENT_DOWN_MS = 2_500;
/** Events older than this (by their wall clock) are history, not something to flash now. */
export const RECENT_EVENT_MS = 60_000;

function workerOf(e: LiveEvent): number | null {
  const w = e.detail?.worker_id;
  return typeof w === 'number' && Number.isInteger(w) && w >= 0 ? w : null;
}

function oldestFirst(events: LiveEvent[]): LiveEvent[] {
  // LiveState.events is newest first.
  return events.toReversed();
}

export class ControllerFlash {
  private owners: Int16Array | null = null;
  private workers = 20;
  private seen = new Set<string>();
  private lastDownMs = Number.NEGATIVE_INFINITY;

  /** Start from the default blocks (a new run, a reset, a new layout) and replay what `history` says. */
  reset(size: number, workers: number, history: LiveEvent[]): void {
    this.owners = defaultOwners(size, workers);
    this.workers = workers;
    this.seen = new Set();
    this.lastDownMs = Number.NEGATIVE_INFINITY;
    for (const e of oldestFirst(history)) {
      this.seen.add(e.id);
      this.replay(e);
    }
  }

  get ready(): boolean {
    return this.owners !== null;
  }

  private replay(e: LiveEvent): void {
    const w = workerOf(e);
    const by = e.detail?.adopted_by;
    if (this.owners && e.kind === 'worker_adopted' && w !== null && Array.isArray(by)) {
      applyAdoption(
        this.owners,
        w,
        by.filter((x): x is number => typeof x === 'number'),
      );
    } else if (this.owners && e.kind === 'worker_recovered' && w !== null) {
      applyRecovery(this.owners, w, this.workers);
    }
  }

  /**
   * Take in this update's events and its decoded codes. Marks a crashed controller's batteries as
   * down in `codes` when the frame missed it; returns how many were marked.
   */
  apply(events: LiveEvent[], codes: Uint8Array, nowMs: number): number {
    if (!this.owners || this.owners.length !== codes.length) return 0;
    let anyDown = false;
    for (let i = 0; i < codes.length; i++) {
      if (codes[i] === DOWN) {
        anyDown = true;
        break;
      }
    }
    if (anyDown) this.lastDownMs = nowMs;

    const cohorts: Int32Array[] = [];
    const flashing = new Set<number>();
    for (const e of oldestFirst(events)) {
      if (this.seen.has(e.id)) continue;
      this.seen.add(e.id);
      const w = workerOf(e);
      const wall = Date.parse(e.wall_utc);
      const recent = !Number.isFinite(wall) || nowMs - wall < RECENT_EVENT_MS;
      if (w !== null && WORKER_EVENTS.has(e.kind) && recent && !flashing.has(w)) {
        flashing.add(w);
        cohorts.push(cohortOf(this.owners, w)); // before any hand-over in this batch
      }
      this.replay(e);
    }

    if (!cohorts.length || anyDown || nowMs - this.lastDownMs < RECENT_DOWN_MS) return 0;
    let marked = 0;
    for (const cohort of cohorts) {
      for (const g of cohort) {
        if (codes[g] !== OFFLINE) {
          codes[g] = DOWN;
          marked++;
        }
      }
    }
    if (marked) this.lastDownMs = nowMs;
    return marked;
  }
}
