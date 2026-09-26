// "Charge by controller", the map's centre-right panel: one battery gauge per controller, filled to the usable
// charge of the batteries it runs (docs/API.md Worker.usable_charge: the energy left above the 20% reserve,
// 0-100%), with its status. The failure model (docs/API.md): a crash empties a controller's batteries, so its
// gauge drops to 0% with a red ✕ and stays there until someone restarts it; taking its batteries offline halves
// their charge, and the gauge keeps a ghost mark where it was. Pure, so it is unit tested; ChargePanel.tsx
// draws it.

import type { Worker, WorkerStatus } from '../../../api/types.ts';
import { cityName } from '../../../lib/cities.ts';
import { controllerName } from '../../../lib/fleet.ts';
import { fmtCount, fmtList, plural } from '../../../lib/format.ts';
import { workerShare, type Scope } from './scope.ts';

/** Every battery keeps this much charge in reserve; usable charge is what is above it. */
export const RESERVE = 0.2;

export type GaugeLook = 'working' | 'suspect' | 'crashed' | 'starting';

const LOOKS: Record<WorkerStatus, GaugeLook> = { healthy: 'working', suspect: 'suspect', dead: 'crashed', starting: 'starting' };

export interface Gauge {
  workerId: number;
  /** Controller N, as people count them (worker_id + 1). */
  number: number;
  look: GaugeLook;
  /** How far up the gauge is filled, 0-1: the usable charge; 0 once it crashed; null when not known. */
  fill: number | null;
  /** "52%", "0%", "—" (it runs no batteries), or null when not known. */
  value: string | null;
  /** Its batteries were taken offline (the drawer): half their charge is gone and the rest holds still. */
  offline: boolean;
  /** Where the gauge stood before its batteries went offline (twice today's level); null when there is nothing to mark. */
  ghost: number | null;
  /** It runs batteries in the selected city; null for all of Texas. */
  serving: boolean | null;
  /** Batteries it runs: in the selected city, or in all of Texas. */
  batteries: number;
  /** Where its batteries are: "Houston", "San Antonio and Austin". */
  where: string | null;
}

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const clamp01 = (n: number) => Math.min(1, Math.max(0, n));

/**
 * A controller's usable charge, 0-1: `usable_charge`, else worked out from `charge_level` (an older backend).
 * null when it runs no batteries; undefined when not known (before this page was opened, or not reported).
 */
export function usableCharge(w: Pick<Worker, 'usable_charge' | 'charge_level'>): number | null | undefined {
  if (finite(w.usable_charge)) return clamp01(w.usable_charge);
  if (w.usable_charge === null) return null;
  if (finite(w.charge_level)) return clamp01((w.charge_level - RESERVE) / (1 - RESERVE));
  if (w.charge_level === null) return null;
  return undefined;
}

/** "52%" (whole percent), "—" when it runs no batteries, null when not known. */
export function fmtCharge(usable: number | null | undefined): string | null {
  if (usable === undefined) return null;
  if (usable === null) return '—';
  return `${Math.round(clamp01(usable) * 100)}%`;
}

/** "Houston", "San Antonio and Austin": the cities a controller runs batteries in now. */
function whereOf(w: Worker): string | null {
  const locs = Object.entries(w.by_location ?? {})
    .filter(([, n]) => finite(n) && n > 0)
    .map(([loc]) => cityName(loc));
  return locs.length ? fmtList(locs) : null;
}

/** One gauge per controller, in controller order. */
export function buildGauges(workers: readonly Worker[], scope: Scope, offlineWorkers: readonly number[] = []): Gauge[] {
  const offline = new Set(offlineWorkers);
  return workers
    .toSorted((a, b) => a.worker_id - b.worker_id)
    .map((w) => {
      const look = LOOKS[w.status] ?? 'starting';
      const usable = usableCharge(w);
      const isOffline = offline.has(w.worker_id);
      const known = usable !== undefined;
      // A crash empties its batteries: 0%, whatever arrives before the hand-over.
      const fill = !known ? null : look === 'crashed' ? 0 : (usable ?? 0);
      // Going offline halved the charge, and it holds still: it stood at twice today's level.
      const ghost = isOffline && finite(usable) && look !== 'crashed' && usable > 0.005 ? Math.min(1, usable * 2) : null;
      return {
        workerId: w.worker_id,
        number: w.worker_id + 1,
        look,
        fill,
        value: look === 'crashed' && finite(usable) ? '0%' : fmtCharge(usable),
        offline: isOffline,
        ghost,
        serving: scope === null ? null : workerShare(w, scope) > 0,
        batteries: workerShare(w, scope),
        where: whereOf(w),
      };
    });
}

export interface GaugeSummary {
  total: number;
  working: number;
  suspect: number;
  crashed: number;
  starting: number;
  offline: number;
  /** How many serve the selected city; null for all of Texas. */
  serving: number | null;
  /** Whether any charge is known (not before this page was opened, nor from a backend that does not say). */
  known: boolean;
}

export function summarize(gauges: readonly Gauge[]): GaugeSummary {
  const count = (look: GaugeLook) => gauges.filter((g) => g.look === look).length;
  const scoped = gauges.some((g) => g.serving !== null);
  return {
    total: gauges.length,
    working: count('working'),
    suspect: count('suspect'),
    crashed: count('crashed'),
    starting: count('starting'),
    offline: gauges.filter((g) => g.offline).length,
    serving: scoped ? gauges.filter((g) => g.serving).length : null,
    known: gauges.some((g) => g.fill !== null),
  };
}

// ---- words ---------------------------------------------------------------------------------------

/** What a click on the gauge does: crash a working controller, restart a crashed one, or nothing (and why). */
export interface GaugeAction {
  action: 'crash' | 'restart' | null;
  /** "Click to crash it", or why it can't be done now. */
  hint: string;
}

export interface GaugeContext {
  /** The timeline shows an earlier moment. */
  past: boolean;
  /** A run exists (running or paused). */
  running: boolean;
  /** Controllers working now (the server keeps at least 2). */
  working: number;
}

export function gaugeAction(g: Gauge, ctx: GaugeContext): GaugeAction {
  if (ctx.past) return { action: null, hint: 'Go back to live to crash or restart a controller' };
  if (!ctx.running) return { action: null, hint: 'Start a simulation first' };
  switch (g.look) {
    case 'crashed':
      return { action: 'restart', hint: 'Click to restart it' };
    case 'working':
      return ctx.working <= 2 ? { action: null, hint: 'At least 2 controllers must keep working' } : { action: 'crash', hint: 'Click to crash it' };
    case 'suspect':
      return { action: null, hint: 'It is marked crashed if it misses the next step' };
    default:
      return { action: null, hint: 'It is starting' };
  }
}

/** The gauge's tip: "Controller 7 · 52%", then what is going on with it. */
export function gaugeWords(g: Gauge, scope: Scope): { title: string; detail: string } {
  const title = `${controllerName(g.workerId)}${g.value === null ? '' : ` · ${g.value}`}`;
  const runs = `${fmtCount(g.batteries)} ${plural(g.batteries, 'battery', 'batteries')}`;
  const where = scope === null ? (g.where ? ` in ${g.where}` : '') : ` in ${cityName(scope)}`;
  let detail: string;
  switch (g.look) {
    case 'crashed':
      detail =
        g.value === '—'
          ? 'Crashed. Its batteries were emptied and handed to the other controllers.'
          : 'Crashed. Its batteries were emptied; the other controllers are taking them over.';
      break;
    case 'suspect':
      detail = `Not responding. Its ${runs}${where} get no orders.`;
      break;
    case 'starting':
      detail = 'Restarting. It takes its batteries back in a moment.';
      break;
    default:
      if (!g.offline) detail = `Working, running ${runs}${where}.`;
      else if (g.ghost !== null) {
        detail = `Its ${runs}${where} are offline: half their charge was lost (it had ${Math.round(g.ghost * 100)}%), and the rest holds still.`;
      } else detail = `Its ${runs}${where} are offline and hold still.`;
  }
  if (g.value === null && g.look !== 'crashed') detail += ' Charge not known.';
  return { title, detail };
}

// ---- sudden changes, for the animation --------------------------------------------------------------

export interface GaugeChange {
  kind: 'drop' | 'refill';
  /** Where the gauge stood before. */
  was: number;
}

/**
 * What changed suddenly between two frames of the same run, to animate: a controller that crashed or stopped
 * responding, or whose batteries went offline, drops (when its charge fell); a restarted one refills (when it
 * came back with some charge). Ordinary trading never counts, however fast the replay runs, and unknown charge
 * never animates. `peak` is the highest each gauge stood lately: a crash can empty the batteries a frame before
 * the controller is reported down (while paused, say), and the drop is from where it stood before that.
 */
export function gaugeChanges(prev: readonly Gauge[], next: readonly Gauge[], peak: ReadonlyMap<number, number> = new Map()): Map<number, GaugeChange> {
  const before = new Map(prev.map((g) => [g.workerId, g]));
  const out = new Map<number, GaugeChange>();
  for (const g of next) {
    const p = before.get(g.workerId);
    if (!p || p.fill === null || g.fill === null) continue;
    const was = Math.max(p.fill, peak.get(g.workerId) ?? 0);
    const stopped = (p.look === 'working' && (g.look === 'crashed' || g.look === 'suspect')) || (!p.offline && g.offline);
    // Back from a restart with its batteries (not the start of a run, when the charge does not jump).
    const back = (p.look === 'crashed' || p.look === 'starting') && g.look === 'working';
    if (stopped && was - g.fill > 0.005) out.set(g.workerId, { kind: 'drop', was });
    else if (back && g.fill - p.fill > 0.005) out.set(g.workerId, { kind: 'refill', was: p.fill });
  }
  return out;
}
