// What this page has seen of the run, for the timeline (docs/UX.md, "Live map v2"): for each frame, the
// battery_status (the map's dots at that moment) and the controllers, availability and race then, keyed by the
// sim time of the frame's latest tick. The past before the page was opened is not here: the corners rebuild it
// from the tick history (src/live/past.ts) and the map shows city totals only.
//
// Memory: battery_status is kept as its packed bytes (4 bits a battery, 10 KB for 20,000), shared between
// frames that did not change (a pause). Past the cap (8 MB, about 800 frames of 20,000 batteries: 13 minutes
// at one frame a second) every other record in the older half is dropped, so the whole visit stays scrubbable,
// the older part a little coarser. Pure, so it is unit tested.

import type { Availability, Race, Worker } from '../api/types.ts';
import { base64ToBytes } from '../map/layout.ts';
import type { LiveState } from './reducer.ts';

export interface FrameRecord {
  /** Sim time of the frame's latest tick, ms. */
  tMs: number;
  /** battery_status as packed bytes, or null when the frame had none. */
  status: Uint8Array | null;
  workers: Worker[];
  availability: Availability | null;
  race: Race | null;
  /** The page stopped listening here (it left the Live page): nothing is known until the next record. */
  gap?: boolean;
}

export const RECORDER_MAX_BYTES = 8 * 1024 * 1024;

export class FrameRecorder {
  records: FrameRecord[] = [];
  /** Bumped on every change, for subscribers. */
  version = 0;
  private readonly maxBytes: number;
  private bytes = 0;
  private runId: string | null = null;
  private lastB64: string | null = null;

  constructor(maxBytes = RECORDER_MAX_BYTES) {
    this.maxBytes = maxBytes;
  }

  get sizeBytes(): number {
    return this.bytes;
  }

  clear(): void {
    if (!this.records.length && this.runId === null) return;
    this.records = [];
    this.bytes = 0;
    this.lastB64 = null;
    this.runId = null;
    this.version++;
  }

  /** Take in the live state after a snapshot or frame; true when a record was added or replaced. */
  add(state: LiveState): boolean {
    if (state.runId !== this.runId) {
      this.clear();
      this.runId = state.runId;
    }
    if (!state.runId || state.status === 'idle' || state.status === 'starting') return false;
    const last = state.history[state.history.length - 1];
    const tMs = last ? Date.parse(last.ts_utc) : Number.NaN;
    if (!Number.isFinite(tMs)) return false;

    const prev = this.records[this.records.length - 1];
    let status: Uint8Array | null = null;
    if (state.batteryStatus) {
      if (prev && !prev.gap && state.batteryStatus === this.lastB64) status = prev.status;
      else {
        status = base64ToBytes(state.batteryStatus);
        if (status) this.bytes += status.length;
      }
    }
    this.lastB64 = state.batteryStatus;
    const record: FrameRecord = { tMs, status, workers: state.workers, availability: state.availability, race: state.race };

    if (prev && !prev.gap && prev.tMs === tMs) {
      // The same moment again (paused, or a frame with no new tick): the latest view of it wins.
      if (prev.status && prev.status !== status && !this.shared(prev.status, this.records.length - 1)) this.bytes -= prev.status.length;
      this.records[this.records.length - 1] = record;
    } else if (prev && tMs < prev.tMs) {
      return false; // older than what we hold (a late snapshot): ignore
    } else {
      this.records.push(record);
    }
    if (this.bytes > this.maxBytes) this.thin();
    this.version++;
    return true;
  }

  /** The page stopped listening (it left the Live page): what follows is unknown until the next record. */
  gap(): void {
    const prev = this.records[this.records.length - 1];
    if (!prev || prev.gap) return;
    this.records.push({ tMs: prev.tMs + 1, status: null, workers: prev.workers, availability: null, race: null, gap: true });
    this.lastB64 = null;
    this.version++;
  }

  /** The recorded view at `tMs`: the latest record at or before it; null before the first one or in a gap. */
  at(tMs: number): FrameRecord | null {
    let lo = 0;
    let hi = this.records.length - 1;
    let found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (this.records[mid].tMs <= tMs) {
        found = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    const r = found >= 0 ? this.records[found] : null;
    return r && !r.gap ? r : null;
  }

  /** When the page's recording starts (the first record's moment), or null. */
  firstMs(): number | null {
    return this.records.length ? this.records[0].tMs : null;
  }

  private shared(status: Uint8Array, index: number): boolean {
    return this.records[index - 1]?.status === status || this.records[index + 1]?.status === status;
  }

  /** Drop every other record in the older half (never a gap, never the first), then recount the bytes. */
  private thin(): void {
    while (this.bytes > this.maxBytes && this.records.length > 4) {
      const half = Math.floor(this.records.length / 2);
      this.records = this.records.filter((r, i) => i === 0 || i >= half || r.gap || i % 2 === 0);
      const seen = new Set<Uint8Array>();
      let bytes = 0;
      for (const r of this.records) {
        if (r.status && !seen.has(r.status)) {
          seen.add(r.status);
          bytes += r.status.length;
        }
      }
      if (bytes >= this.bytes) break; // nothing left to drop
      this.bytes = bytes;
    }
  }
}
