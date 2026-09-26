// SIMULATED fleet for the mock API (dev and tests only; never shipped).
//
// Real: the LZ_HOUSTON / LZ_NORTH prices in ./data (ERCOT, May 16-17, 2025).
// Simulated: everything else — batteries, controllers, decisions, money,
// forecasts, failures and full-year results. The physics follow RUNLOG.md
// loosely (grid-side metering, sqrt(eta) each way, 20% reserve) so the site
// sees realistic shapes, but no number here is a real result.

import type {
  Availability,
  AvailabilityRequest,
  AvailabilityResponse,
  BatteryInfo,
  EditableSettings,
  FailedCommand,
  FrameMessage,
  Forecast,
  IdleMessage,
  LiveEvent,
  Preset,
  Results,
  RunStatus,
  Settings,
  Severity,
  SnapshotMessage,
  Tick,
  TickAction,
  Totals,
  Worker,
} from '../src/api/types.ts';
import priceFixture from './data/ercot_rt_lz_2025-05-16_17.json' with { type: 'json' };

/** Present in every mock module so scripts/check-dist.mjs can prove none of it ships. */
export const MOCK_MARKER = 'FLEET_SIM_MOCK_DO_NOT_SHIP';
export const MOCK_VERSION = 'mock-0.1 (simulated data; real ERCOT prices for 2025-05-16/17)';

export const LOCS = ['LZ_HOUSTON', 'LZ_NORTH'] as const;
export type Loc = (typeof LOCS)[number];

const TICK_S = 300;
const INTERVAL_S = 900;
const DT_H = TICK_S / 3600;
const FLEET = 20_000;
const PER_AREA = 10_000;
const WORKERS = 20;
const PER_WORKER = 1_000;
const SQRT_ETA = Math.sqrt(0.9);
const SOC_MIN = 0.2;
const SOC_MAX = 1.0;
const HISTORY_CAP = 2000;
const EVENTS_CAP = 50;
const FORECAST_EVERY_TICKS = 12;
const FORECAST_HORIZON_S = 4 * 3600;

export const PRESETS: Preset[] = [
  {
    id: 'spike_evening',
    label: 'Price-spike evening',
    date_local: '2025-05-16',
    start_utc: '2025-05-16T21:00:00Z',
    description: 'Prices jump above $2,000/MWh around 7:45 pm.',
  },
  {
    id: 'morning_spike',
    label: 'Biggest spike of 2025',
    date_local: '2025-04-07',
    start_utc: '2025-04-07T08:00:00Z',
    description: "The year's highest price, just before 7 am.",
  },
  {
    id: 'winter_spike',
    label: 'Winter morning spike',
    date_local: '2025-01-15',
    start_utc: '2025-01-15T10:00:00Z',
    description: 'A cold-morning price jump around 7:30 am.',
  },
  {
    id: 'ordinary_day',
    label: 'An ordinary day',
    date_local: '2025-03-19',
    start_utc: '2025-03-19T05:00:00Z',
    description: 'A typical day with small ups and downs.',
  },
];

export const DEFAULT_SETTINGS: Settings = {
  preset: 'spike_evening',
  start_utc: '2025-05-16T21:00:00Z',
  speed_tps: 2,
  strategy: 'forecast',
  start_charge_level: 0.9,
  fleet_size: FLEET,
  power_kw_range: [5.0, 11.5],
  capacity_kwh_range: [10.0, 20.0],
  locations: { LZ_HOUSTON: 0.5, LZ_NORTH: 0.5 },
};

export const SPEEDS = [1, 2, 5, 10];
export const STRATEGIES = [
  { value: 'bands', label: 'Simple rule' },
  { value: 'forecast', label: 'AI forecast (TimesFM-3)' },
  { value: 'peak', label: 'Evening peak' },
];

export class MockError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfterS: number | null;
  constructor(status: number, code: string, message: string, retryAfterS: number | null = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryAfterS = retryAfterS;
  }
}

// ---- real prices ---------------------------------------------------------------

interface PriceRow {
  ts_utc: string;
  ts_local: string;
  LZ_HOUSTON: number;
  LZ_NORTH: number;
}

const ROWS: PriceRow[] = priceFixture.rows as PriceRow[];
const toS = (iso: string) => Math.round(Date.parse(iso) / 1000);
const FIXTURE_START_S = toS(ROWS[0].ts_utc);
const FIXTURE_END_S = toS(ROWS[ROWS.length - 1].ts_utc) + INTERVAL_S;

function rowAt(tsS: number): PriceRow | null {
  const i = Math.floor((tsS - FIXTURE_START_S) / INTERVAL_S);
  return i >= 0 && i < ROWS.length ? ROWS[i] : null;
}

/** 15-minute price, forward-filled within its own interval only. */
export function priceAt(loc: Loc, tsS: number): number | null {
  const row = rowAt(tsS);
  return row ? row[loc] : null;
}

function isoUtc(tsS: number): string {
  return new Date(tsS * 1000).toISOString().replace('.000Z', 'Z');
}

function isoLocal(tsS: number): string {
  const row = rowAt(tsS) ?? ROWS[ROWS.length - 1];
  const offset = row.ts_local.slice(-6); // e.g. -05:00
  const sign = offset.startsWith('-') ? -1 : 1;
  const [h, m] = offset.slice(1).split(':').map(Number);
  const local = new Date((tsS + sign * (h * 3600 + m * 60)) * 1000).toISOString().slice(0, 19);
  return `${local}${offset}`;
}

function localHour(tsS: number): number {
  const local = isoLocal(tsS);
  return Number(local.slice(11, 13)) + Number(local.slice(14, 16)) / 60;
}

function round(n: number, decimals = 2): number {
  return Math.round(n * 10 ** decimals) / 10 ** decimals;
}

function percentile(values: number[], p: number): number {
  const sorted = values.toSorted((a, b) => a - b);
  if (!sorted.length) return 0;
  const idx = (sorted.length - 1) * p;
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

const CHEAP: Record<Loc, number> = {
  LZ_HOUSTON: percentile(ROWS.map((r) => r.LZ_HOUSTON), 0.3),
  LZ_NORTH: percentile(ROWS.map((r) => r.LZ_NORTH), 0.3),
};

// ---- seeded randomness -------------------------------------------------------------

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffled(n: number, seed: number): Uint16Array {
  const rand = mulberry32(seed);
  const out = new Uint16Array(n);
  for (let i = 0; i < n; i++) out[i] = i;
  for (let i = n - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    const t = out[i];
    out[i] = out[j];
    out[j] = t;
  }
  return out;
}

// ---- the fleet ---------------------------------------------------------------------------

const POWER = new Float64Array(FLEET);
const CAPACITY = new Float64Array(FLEET);
{
  const rand = mulberry32(42);
  for (let g = 0; g < FLEET; g++) {
    const p = 5 + 6.5 * rand();
    const c = 10 + 10 * rand();
    POWER[g] = Math.min(p, c);
    CAPACITY[g] = c;
  }
}
const PERMUTATION: Record<Loc, Uint16Array> = { LZ_HOUSTON: shuffled(PER_AREA, 7), LZ_NORTH: shuffled(PER_AREA, 8) };
const NAMEPLATE_KW = POWER.reduce((a, b) => a + b, 0);

const areaOf = (g: number): Loc => (g < PER_AREA ? 'LZ_HOUSTON' : 'LZ_NORTH');
const areaBase = (loc: Loc) => (loc === 'LZ_HOUSTON' ? 0 : PER_AREA);
export const batteryId = (g: number) => `${areaOf(g)}-${String(g % PER_AREA).padStart(6, '0')}`;

export function batteryIndex(id: string): number | null {
  const m = /^(LZ_HOUSTON|LZ_NORTH)-(\d{6})$/.exec(id);
  if (!m) return null;
  const n = Number(m[2]);
  return n < PER_AREA ? areaBase(m[1] as Loc) + n : null;
}

const AREA_WORDS: Record<Loc, string> = { LZ_HOUSTON: 'Houston-area', LZ_NORTH: 'Dallas–Fort Worth-area' };
const AREA_NAMES: Record<Loc, string> = { LZ_HOUSTON: 'Houston area', LZ_NORTH: 'Dallas–Fort Worth area' };

function money(n: number): string {
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: n >= 100 ? 0 : 2, maximumFractionDigits: n >= 100 ? 0 : 2 })}`;
}
function perKwh(price: number): string {
  const cents = price / 10;
  return Math.abs(cents) >= 100 ? `$${(cents / 100).toFixed(2)} per kWh` : `${cents.toFixed(Math.abs(cents) >= 10 ? 0 : 1)}¢ per kWh`;
}

interface WorkerState extends Worker {
  killedAtTick: number | null;
  killedAtMs: number | null;
  missed: number;
}

interface Job {
  id: string;
  doneAtMs: number;
  fail: boolean;
}

export interface MockSimOptions {
  now?: () => number;
  random?: () => number;
  backtestMs?: number;
  backtestFails?: boolean;
  resultsBeforeBacktest?: boolean;
}

export class MockSim {
  status: RunStatus = 'idle';
  runId: string | null = null;
  settings: Settings = { ...DEFAULT_SETTINGS };

  private readonly now: () => number;
  private readonly random: () => number;
  private readonly backtestMs: number;
  private readonly backtestFails: boolean;
  private resultsReady: boolean;

  private startedAtMs = 0;
  private tickIndex = 0;
  private history: Tick[] = [];
  private totals: Totals = { revenue_usd: 0, sold_kwh: 0, bought_kwh: 0, ticks: 0 };
  private soc = new Float64Array(FLEET);
  private owner = new Uint8Array(FLEET);
  private offLocation = new Uint8Array(FLEET);
  private offWorker = new Uint8Array(FLEET);
  private offBattery = new Uint8Array(FLEET);
  private lastSetpoint = new Float64Array(FLEET);
  private workers: WorkerState[] = [];
  private offlineWorkers = new Set<number>();
  private locationFraction: Record<Loc, number> = { LZ_HOUSTON: 0, LZ_NORTH: 0 };
  private failed: { count: number; recent: FailedCommand[] } = { count: 0, recent: [] };
  private events: LiveEvent[] = [];
  private pendingEvents: LiveEvent[] = [];
  private pendingTicks: Tick[] = [];
  private forecast: Partial<Record<Loc, Forecast>> = {};
  private forecastLog: Forecast[] = [];
  private pendingForecast: Partial<Record<Loc, Forecast>> | null = null;
  private lastAction: Record<Loc, TickAction> = { LZ_HOUSTON: 'hold', LZ_NORTH: 'hold' };
  private spiked: Record<Loc, boolean> = { LZ_HOUSTON: false, LZ_NORTH: false };
  private eventSeq = 0;
  private pidSeq = 4100;
  private jobs = new Map<string, Job>();
  private currentTickUtc: string | null = null;

  constructor(options: MockSimOptions = {}) {
    this.now = options.now ?? Date.now;
    this.random = options.random ?? mulberry32(1234);
    this.backtestMs = options.backtestMs ?? 6000;
    this.backtestFails = options.backtestFails ?? false;
    this.resultsReady = options.resultsBeforeBacktest ?? true;
    this.resetFleet();
  }

  // ---- lifecycle ---------------------------------------------------------------

  private resetFleet(startCharge = DEFAULT_SETTINGS.start_charge_level): void {
    this.soc.fill(startCharge);
    for (let g = 0; g < FLEET; g++) this.owner[g] = Math.floor(g / PER_WORKER) + 1;
    this.offLocation.fill(0);
    this.offWorker.fill(0);
    this.offBattery.fill(0);
    this.lastSetpoint.fill(0);
    this.offlineWorkers.clear();
    this.locationFraction = { LZ_HOUSTON: 0, LZ_NORTH: 0 };
    this.workers = Array.from({ length: WORKERS }, (_, i) => ({
      worker_id: i + 1,
      status: 'healthy' as const,
      pid: this.pidSeq++,
      batteries: PER_WORKER,
      last_tick_utc: null,
      adopted_from: [],
      killedAtTick: null,
      killedAtMs: null,
      missed: 0,
    }));
  }

  start(partial: Partial<EditableSettings>): { run_id: string; settings: Settings } {
    const next: Settings = { ...DEFAULT_SETTINGS };
    if (partial.preset !== undefined) {
      const preset = PRESETS.find((p) => p.id === partial.preset);
      if (!preset) throw new MockError(400, 'unknown_preset', `There is no replay called "${partial.preset}".`);
      if (preset.id !== 'spike_evening') {
        throw new MockError(
          501,
          'not_in_mock',
          'The practice server only has prices for May 16–17, 2025. Pick "Price-spike evening".',
        );
      }
      next.preset = preset.id;
      next.start_utc = preset.start_utc ?? next.start_utc;
    }
    if (partial.speed_tps !== undefined) {
      if (!SPEEDS.includes(Number(partial.speed_tps))) {
        throw new MockError(400, 'bad_speed', 'Speed must be 1, 2, 5 or 10 steps per second.');
      }
      next.speed_tps = Number(partial.speed_tps);
    }
    if (partial.strategy !== undefined) {
      if (!STRATEGIES.some((s) => s.value === partial.strategy)) {
        throw new MockError(400, 'bad_strategy', 'Pick one of the three decision methods.');
      }
      next.strategy = partial.strategy;
    }
    if (partial.start_charge_level !== undefined) {
      const c = Number(partial.start_charge_level);
      if (!(c >= SOC_MIN && c <= SOC_MAX)) {
        throw new MockError(400, 'bad_charge', 'The starting charge must be between 20% and 100%.');
      }
      next.start_charge_level = c;
    }
    this.settings = next;
    this.runId = crypto.randomUUID();
    this.status = 'starting';
    this.startedAtMs = this.now();
    this.tickIndex = 0;
    this.history = [];
    this.pendingTicks = [];
    this.totals = { revenue_usd: 0, sold_kwh: 0, bought_kwh: 0, ticks: 0 };
    this.failed = { count: 0, recent: [] };
    this.events = [];
    this.pendingEvents = [];
    this.forecast = {};
    this.forecastLog = [];
    this.pendingForecast = null;
    this.lastAction = { LZ_HOUSTON: 'hold', LZ_NORTH: 'hold' };
    this.spiked = { LZ_HOUSTON: false, LZ_NORTH: false };
    this.resetFleet(next.start_charge_level);
    for (const w of this.workers) w.status = 'starting';
    return { run_id: this.runId, settings: this.settings };
  }

  pause(): { status: RunStatus } {
    if (this.status === 'running') {
      this.status = 'paused';
      this.emit('run_paused', 'info', 'The replay is paused. Press Resume to carry on.');
    }
    return { status: this.status };
  }

  resume(): { status: RunStatus } {
    if (this.status === 'paused') {
      this.status = 'running';
      this.emit('run_resumed', 'info', 'The replay is running again.');
    }
    return { status: this.status };
  }

  setSpeed(speed: number): { speed_tps: number } {
    if (!SPEEDS.includes(speed)) throw new MockError(400, 'bad_speed', 'Speed must be 1, 2, 5 or 10 steps per second.');
    if (speed !== this.settings.speed_tps) {
      this.settings = { ...this.settings, speed_tps: speed };
      const secs = 12 / speed;
      this.emit('speed_changed', 'info', `Speed changed: 1 hour of market time now takes ${secs} seconds.`, { speed_tps: speed });
    }
    return { speed_tps: speed };
  }

  reset(): { status: RunStatus } {
    this.status = 'idle';
    this.runId = null;
    this.history = [];
    this.pendingTicks = [];
    this.totals = { revenue_usd: 0, sold_kwh: 0, bought_kwh: 0, ticks: 0 };
    this.failed = { count: 0, recent: [] };
    this.forecast = {};
    this.pendingForecast = null;
    this.events = [];
    this.pendingEvents = [];
    this.resetFleet();
    this.emit('run_reset', 'good', 'The simulation was reset: all 20 controllers restarted and every battery is back online.');
    return { status: 'idle' };
  }

  // ---- availability ------------------------------------------------------------

  private isAvailable(g: number): boolean {
    return !this.offLocation[g] && !this.offWorker[g] && !this.offBattery[g];
  }

  private countAvailable(): { total: number; byLoc: Record<Loc, number> } {
    const byLoc: Record<Loc, number> = { LZ_HOUSTON: 0, LZ_NORTH: 0 };
    for (let g = 0; g < FLEET; g++) if (this.isAvailable(g)) byLoc[areaOf(g)]++;
    return { total: byLoc.LZ_HOUSTON + byLoc.LZ_NORTH, byLoc };
  }

  availability(): Availability {
    const { total, byLoc } = this.countAvailable();
    return {
      available: total,
      total: FLEET,
      by_location: {
        LZ_HOUSTON: { available: byLoc.LZ_HOUSTON, total: PER_AREA },
        LZ_NORTH: { available: byLoc.LZ_NORTH, total: PER_AREA },
      },
      offline_workers: [...this.offlineWorkers].toSorted((a, b) => a - b),
    };
  }

  setAvailability(req: AvailabilityRequest): AvailabilityResponse {
    let affected = 0;
    let message = '';
    switch (req?.scope) {
      case 'location': {
        const loc = req.location as Loc;
        const f = Number(req.offline_fraction);
        if (!LOCS.includes(loc)) throw new MockError(400, 'bad_location', 'Unknown area. Use LZ_HOUSTON or LZ_NORTH.');
        if (!(f >= 0 && f <= 1)) throw new MockError(400, 'bad_fraction', 'The offline share must be between 0 and 1.');
        const base = areaBase(loc);
        for (let n = 0; n < PER_AREA; n++) this.offLocation[base + n] = 0;
        affected = Math.round(f * PER_AREA);
        const perm = PERMUTATION[loc];
        for (let n = 0; n < affected; n++) this.offLocation[base + perm[n]] = 1;
        this.locationFraction[loc] = f;
        message =
          affected === 0
            ? `All ${AREA_NAMES[loc]} batteries are available again.`
            : `${affected.toLocaleString('en-US')} ${AREA_WORDS[loc]} batteries were taken offline, so the fleet's plan shrinks.`;
        break;
      }
      case 'worker': {
        const w = this.workers.find((x) => x.worker_id === Number(req.worker_id));
        if (!w) throw new MockError(404, 'unknown_worker', `There is no controller ${req.worker_id}.`);
        const offline = req.available === false;
        for (let g = 0; g < FLEET; g++) {
          if (this.owner[g] === w.worker_id) {
            this.offWorker[g] = offline ? 1 : 0;
            affected++;
          }
        }
        if (offline) this.offlineWorkers.add(w.worker_id);
        else this.offlineWorkers.delete(w.worker_id);
        message = offline
          ? `Controller ${w.worker_id}'s ${affected.toLocaleString('en-US')} batteries were taken offline. The controller itself keeps running.`
          : `Controller ${w.worker_id}'s batteries are back online.`;
        break;
      }
      case 'battery': {
        const g = batteryIndex(String(req.battery_id));
        if (g === null) throw new MockError(404, 'unknown_battery', `No battery has the ID ${req.battery_id}.`);
        this.offBattery[g] = req.available === false ? 1 : 0;
        affected = 1;
        message = `Battery ${req.battery_id} is ${req.available === false ? 'offline' : 'back online'}.`;
        break;
      }
      case 'all': {
        affected = FLEET - this.countAvailable().total;
        this.offLocation.fill(0);
        this.offWorker.fill(0);
        this.offBattery.fill(0);
        this.offlineWorkers.clear();
        this.locationFraction = { LZ_HOUSTON: 0, LZ_NORTH: 0 };
        message = 'Every battery is back online.';
        break;
      }
      default:
        throw new MockError(400, 'bad_scope', 'scope must be location, worker, battery or all.');
    }
    this.emit('availability_changed', 'info', message, { request: req, affected });
    const { total } = this.countAvailable();
    return { applied_from_tick_utc: isoUtc(this.nextTickS()), affected, available: total, total: FLEET };
  }

  battery(id: string): BatteryInfo {
    const g = batteryIndex(id);
    if (g === null) throw new MockError(404, 'unknown_battery', `No battery has the ID ${id}.`);
    return {
      battery_id: batteryId(g),
      location: areaOf(g),
      worker_id: this.owner[g],
      capacity_kwh: Math.round(CAPACITY[g] * 100) / 100,
      power_kw: Math.round(POWER[g] * 100) / 100,
      charge_level: Math.round(this.soc[g] * 1000) / 1000,
      power_kw_now: Math.round(this.lastSetpoint[g] * 100) / 100,
      available: this.isAvailable(g),
    };
  }

  // ---- chaos ---------------------------------------------------------------------

  kill(workerId?: number): { worker_id: number; pid: number; killed_at_utc: string } {
    const alive = this.workers.filter((w) => w.status !== 'dead' && w.killedAtTick === null && w.killedAtMs === null);
    let target: WorkerState | undefined;
    if (workerId === undefined) {
      const healthy = alive.filter((w) => w.status === 'healthy');
      target = healthy[Math.floor(this.random() * healthy.length)];
      if (!target) throw new MockError(409, 'too_few_healthy', 'No healthy controller is left to crash.');
    } else {
      target = this.workers.find((w) => w.worker_id === workerId);
      if (!target) throw new MockError(404, 'unknown_worker', `There is no controller ${workerId}.`);
      if (target.status === 'dead' || target.killedAtTick !== null || target.killedAtMs !== null) {
        throw new MockError(409, 'already_dead', `Controller ${workerId} has already crashed.`);
      }
    }
    if (alive.length - 1 < 2) {
      throw new MockError(409, 'too_few_healthy', 'At least 2 controllers must keep working. Press Reset to bring the others back.');
    }
    target.killedAtTick = this.tickIndex;
    target.killedAtMs = this.now();
    return { worker_id: target.worker_id, pid: target.pid ?? 0, killed_at_utc: new Date(this.now()).toISOString() };
  }

  // ---- the clock -------------------------------------------------------------------

  private startS(): number {
    return toS(this.settings.start_utc);
  }

  private tickS(index: number): number {
    return this.startS() + index * TICK_S;
  }

  private nextTickS(): number {
    return this.tickS(this.tickIndex);
  }

  simTimeUtc(): string {
    const last = this.history[this.history.length - 1];
    return last ? last.ts_utc : this.settings.start_utc;
  }

  /** Called once per wall-clock second: advance the replay and return the frame to broadcast. */
  stepWall(): FrameMessage | IdleMessage {
    const now = this.now();
    if (this.status === 'idle') {
      return {
        type: 'idle',
        status: 'idle',
        events: this.pendingEvents.splice(0),
        availability: this.availability(),
        workers: this.publicWorkers(),
      };
    }
    if (this.status === 'starting' && now - this.startedAtMs >= 1500) {
      this.status = 'running';
      for (const w of this.workers) w.status = 'healthy';
      this.emit(
        'run_started',
        'info',
        `The replay started: real prices from May 16, 2025, from 4 pm, with ${FLEET.toLocaleString('en-US')} batteries ${Math.round(this.settings.start_charge_level * 100)}% charged.`,
      );
    }
    if (this.status === 'running') {
      for (let k = 0; k < this.settings.speed_tps; k++) {
        if (this.nextTickS() >= FIXTURE_END_S) {
          this.status = 'finished';
          this.emit('run_paused', 'info', 'The replay reached the end of the recorded prices on this practice server.');
          break;
        }
        this.tick();
      }
    } else if (this.status === 'paused') {
      this.detectWhilePaused(now);
    }
    const forecast = this.pendingForecast;
    this.pendingForecast = null;
    const frame: FrameMessage = {
      type: 'frame',
      run_id: this.runId,
      status: this.status,
      sim_time_utc: this.simTimeUtc(),
      speed_tps: this.settings.speed_tps,
      ticks: this.pendingTicks.splice(0),
      totals: { ...this.totals },
      workers: this.publicWorkers(),
      failed_commands: { count: this.failed.count, recent: [...this.failed.recent] },
      availability: this.availability(),
      events: this.pendingEvents.splice(0),
    };
    if (forecast) frame.forecast = forecast;
    return frame;
  }

  snapshot(): SnapshotMessage {
    const running = this.status !== 'idle';
    return {
      type: 'snapshot',
      run_id: this.runId,
      status: this.status,
      settings: running ? this.settings : null,
      history: this.history.slice(-HISTORY_CAP),
      sim_time_utc: running ? this.simTimeUtc() : undefined,
      speed_tps: this.settings.speed_tps,
      totals: { ...this.totals },
      workers: this.publicWorkers(),
      failed_commands: { count: this.failed.count, recent: [...this.failed.recent] },
      availability: this.availability(),
      events: this.events.slice(-20),
      forecast: { ...this.forecast },
    };
  }

  private publicWorkers(): Worker[] {
    return this.workers.map(({ worker_id, status, pid, batteries, last_tick_utc, adopted_from }) => ({
      worker_id,
      status,
      pid,
      batteries,
      last_tick_utc,
      adopted_from: [...adopted_from],
    }));
  }

  private emit(kind: string, severity: Severity, message: string, detail: Record<string, unknown> = {}): void {
    const last = this.history[this.history.length - 1];
    const simUtc = this.currentTickUtc ?? last?.ts_utc ?? (this.status === 'idle' ? null : this.settings.start_utc);
    const event: LiveEvent = {
      id: `mock-${++this.eventSeq}`,
      wall_utc: new Date(this.now()).toISOString(),
      sim_utc: simUtc,
      kind,
      severity,
      message,
      detail: { simulated: true, ...detail },
    };
    this.events.push(event);
    if (this.events.length > EVENTS_CAP) this.events.splice(0, this.events.length - EVENTS_CAP);
    this.pendingEvents.push(event);
  }

  // ---- decisions (simulated strategies) ------------------------------------------------

  private decide(loc: Loc, tsS: number, decisionPrice: number): TickAction {
    const strategy = this.settings.strategy;
    if (strategy === 'peak') {
      const h = localHour(tsS);
      if (h >= 10 && h < 15) return 'charge';
      if (h >= 18 && h < 21) return 'sell';
      return 'hold';
    }
    if (strategy === 'bands') {
      const past: number[] = [];
      for (let s = FIXTURE_START_S; s < tsS - INTERVAL_S; s += INTERVAL_S) {
        const p = priceAt(loc, s);
        if (p !== null) past.push(p);
      }
      if (past.length < 12) return 'hold';
      if (decisionPrice <= percentile(past, 0.25)) return 'charge';
      if (decisionPrice >= percentile(past, 0.9) && decisionPrice > 0) return 'sell';
      return 'hold';
    }
    // "forecast": peeks at the fixture's next 4 hours, standing in for a good forecast.
    let ahead = decisionPrice;
    for (let s = tsS; s < tsS + FORECAST_HORIZON_S; s += INTERVAL_S) {
      const p = priceAt(loc, s);
      if (p !== null && p > ahead) ahead = p;
    }
    if (decisionPrice >= 100 && decisionPrice >= 0.45 * ahead) return 'sell';
    if (decisionPrice <= CHEAP[loc] && ahead < 3 * CHEAP[loc] + 60) return 'charge';
    return 'hold';
  }

  // ---- one 5-minute step ------------------------------------------------------------------

  private tick(): void {
    const index = this.tickIndex;
    const tsS = this.tickS(index);
    this.currentTickUtc = isoUtc(tsS);
    this.detectAtTick();

    const price: Record<Loc, number> = { LZ_HOUSTON: 0, LZ_NORTH: 0 };
    const decision: Record<Loc, number> = { LZ_HOUSTON: 0, LZ_NORTH: 0 };
    const action: Record<Loc, TickAction> = { LZ_HOUSTON: 'hold', LZ_NORTH: 'hold' };
    const target: Record<Loc, number> = { LZ_HOUSTON: 0, LZ_NORTH: 0 };
    const actual: Record<Loc, number> = { LZ_HOUSTON: 0, LZ_NORTH: 0 };
    const revenue: Record<Loc, number> = { LZ_HOUSTON: 0, LZ_NORTH: 0 };
    const level: Record<Loc, number> = { LZ_HOUSTON: 0, LZ_NORTH: 0 };

    // Workers that crashed this step lose their orders (the gap); suspects get none next step.
    const lost = new Set(this.workers.filter((w) => w.killedAtTick !== null && w.status !== 'dead' && w.missed === 1).map((w) => w.worker_id));
    const skipped = new Set(this.workers.filter((w) => w.status === 'suspect' && !lost.has(w.worker_id)).map((w) => w.worker_id));
    const newlyFailed: FailedCommand[] = [];

    for (const loc of LOCS) {
      price[loc] = priceAt(loc, tsS) ?? 0;
      decision[loc] = priceAt(loc, tsS - INTERVAL_S) ?? price[loc];
      let want = this.decide(loc, tsS, decision[loc]);

      // Directional headroom of each available battery, grid-side kW.
      const base = areaBase(loc);
      let headroom = 0;
      const h = new Float64Array(PER_AREA);
      if (want !== 'hold') {
        for (let n = 0; n < PER_AREA; n++) {
          const g = base + n;
          if (!this.isAvailable(g) || skipped.has(this.owner[g])) continue;
          const energy =
            want === 'sell'
              ? ((this.soc[g] - SOC_MIN) * CAPACITY[g] * SQRT_ETA) / DT_H
              : ((SOC_MAX - this.soc[g]) * CAPACITY[g]) / SQRT_ETA / DT_H;
          h[n] = Math.max(0, Math.min(POWER[g], energy));
          headroom += h[n];
        }
      }
      if (want !== 'hold' && headroom < 1) {
        if (this.lastAction[loc] === want) {
          this.emit(
            'action_changed',
            'info',
            want === 'sell'
              ? `${AREA_WORDS[loc]} batteries stopped selling: they are down to their 20% safety reserve.`
              : `${AREA_WORDS[loc]} batteries stopped charging: they are full.`,
            { location: loc, action: 'hold' },
          );
        }
        want = 'hold';
      }
      action[loc] = want;
      const sign = want === 'sell' ? 1 : want === 'charge' ? -1 : 0;
      target[loc] = sign * headroom;

      let delivered = 0;
      for (let n = 0; n < PER_AREA; n++) {
        const g = base + n;
        const setpoint = headroom > 0 ? sign * h[n] : 0;
        if (setpoint !== 0 && lost.has(this.owner[g])) {
          this.lastSetpoint[g] = 0;
          if (newlyFailed.length < 400) {
            newlyFailed.push({
              battery_id: batteryId(g),
              worker_id: this.owner[g],
              reason: `controller ${this.owner[g]} did not respond after 3 tries`,
              tick_utc: isoUtc(tsS),
            });
          }
          this.failed.count++;
          continue;
        }
        const got = setpoint * (1 - 0.002 * this.random());
        this.lastSetpoint[g] = got;
        delivered += got;
        if (got > 0) this.soc[g] -= (got * DT_H) / SQRT_ETA / CAPACITY[g];
        else if (got < 0) this.soc[g] += (-got * DT_H * SQRT_ETA) / CAPACITY[g];
        this.soc[g] = Math.min(SOC_MAX, Math.max(SOC_MIN, this.soc[g]));
      }
      actual[loc] = delivered;
      revenue[loc] = ((delivered * DT_H) / 1000) * price[loc];
      if (delivered > 0) this.totals.sold_kwh += delivered * DT_H;
      else this.totals.bought_kwh += -delivered * DT_H;

      let stored = 0;
      let cap = 0;
      for (let n = 0; n < PER_AREA; n++) {
        stored += this.soc[base + n] * CAPACITY[base + n];
        cap += CAPACITY[base + n];
      }
      level[loc] = stored / cap;

      this.noteAction(loc, want, decision[loc]);
      this.noteSpike(loc, price[loc]);
    }

    if (newlyFailed.length) this.failed.recent = newlyFailed.slice(-10).toReversed().concat(this.failed.recent).slice(0, 10);

    const fleetTarget = target.LZ_HOUSTON + target.LZ_NORTH;
    const fleetActual = actual.LZ_HOUSTON + actual.LZ_NORTH;
    const fleetRevenue = revenue.LZ_HOUSTON + revenue.LZ_NORTH;
    this.totals.revenue_usd += fleetRevenue;
    this.totals.ticks += 1;
    const capH = CAPACITY.subarray(0, PER_AREA).reduce((a, b) => a + b, 0);
    const capN = CAPACITY.subarray(PER_AREA).reduce((a, b) => a + b, 0);
    const t: Tick = {
      ts_utc: isoUtc(tsS),
      ts_local: isoLocal(tsS),
      price: { ...price },
      decision_price: { ...decision },
      action: { ...action },
      target_kw: { LZ_HOUSTON: round(target.LZ_HOUSTON, 1), LZ_NORTH: round(target.LZ_NORTH, 1), fleet: round(fleetTarget, 1) },
      actual_kw: { LZ_HOUSTON: round(actual.LZ_HOUSTON, 1), LZ_NORTH: round(actual.LZ_NORTH, 1), fleet: round(fleetActual, 1) },
      error_pct: round((Math.abs(fleetActual - fleetTarget) / Math.max(Math.abs(fleetTarget), 0.005 * NAMEPLATE_KW)) * 100, 3),
      revenue_usd: { LZ_HOUSTON: round(revenue.LZ_HOUSTON, 4), LZ_NORTH: round(revenue.LZ_NORTH, 4), fleet: round(fleetRevenue, 4) },
      charge_level: {
        LZ_HOUSTON: round(level.LZ_HOUSTON, 4),
        LZ_NORTH: round(level.LZ_NORTH, 4),
        fleet: round((level.LZ_HOUSTON * capH + level.LZ_NORTH * capN) / (capH + capN), 4),
      },
      healthy_workers: this.workers.filter((w) => w.status === 'healthy').length,
    };
    this.history.push(t);
    if (this.history.length > HISTORY_CAP) this.history.splice(0, this.history.length - HISTORY_CAP);
    this.pendingTicks.push(t);
    for (const w of this.workers) if (w.status !== 'dead' && w.killedAtTick === null) w.last_tick_utc = t.ts_utc;
    if (index % FORECAST_EVERY_TICKS === 0) this.issueForecast(tsS);
    this.tickIndex += 1;
    this.currentTickUtc = null;
  }

  private noteAction(loc: Loc, action: TickAction, decisionPrice: number): void {
    const prev = this.lastAction[loc];
    this.lastAction[loc] = action;
    if (prev === action || action === 'hold') return;
    if (action === 'sell') {
      this.emit('action_changed', 'good', `${AREA_WORDS[loc]} batteries started selling at ${money(decisionPrice)}/MWh (${perKwh(decisionPrice)}).`, {
        location: loc,
        action,
        decision_price: decisionPrice,
      });
    } else {
      this.emit('action_changed', 'info', `${AREA_WORDS[loc]} batteries started charging: power is cheap at ${money(decisionPrice)}/MWh (${perKwh(decisionPrice)}).`, {
        location: loc,
        action,
        decision_price: decisionPrice,
      });
    }
  }

  private noteSpike(loc: Loc, price: number): void {
    if (this.spiked[loc] || price < 1000) return;
    this.spiked[loc] = true;
    this.emit('price_spike', 'info', `${AREA_NAMES[loc]} prices spiked to ${money(price)}/MWh (${perKwh(price)}), about ${Math.round(price / CHEAP[loc])} times the overnight price.`, {
      location: loc,
      price,
    });
  }

  private issueForecast(tsS: number): void {
    const first = Math.ceil((tsS + 1) / INTERVAL_S) * INTERVAL_S;
    const issued: Partial<Record<Loc, Forecast>> = {};
    for (const loc of LOCS) {
      const points = [];
      for (let s = first; s < first + FORECAST_HORIZON_S; s += INTERVAL_S) {
        const real = priceAt(loc, s);
        if (real === null) break;
        const hoursAhead = (s - tsS) / 3600;
        const p50 = real * (1 + (this.random() - 0.5) * 0.25);
        const spread = 0.12 + 0.06 * hoursAhead;
        points.push({
          ts_utc: isoUtc(s),
          p10: Math.round(p50 * (1 - spread) * 100) / 100,
          p50: Math.round(p50 * 100) / 100,
          p90: Math.round(p50 * (1 + spread * 1.4) * 100) / 100,
        });
      }
      if (!points.length) continue;
      const f: Forecast = {
        location: loc,
        series: 'rt_price',
        model: 'mock (simulated, not TimesFM)',
        issued_utc: isoUtc(tsS),
        step_minutes: 15,
        points,
      };
      issued[loc] = f;
      this.forecast[loc] = f;
      this.forecastLog.push(f);
    }
    if (this.forecastLog.length > 200) this.forecastLog.splice(0, this.forecastLog.length - 200);
    if (Object.keys(issued).length) {
      this.pendingForecast = { ...this.pendingForecast, ...issued };
      if (this.forecastLog.length <= LOCS.length) {
        this.emit('forecast_issued', 'info', 'A new AI price forecast covers the next 4 hours (on this practice server it is simulated).');
      }
    }
  }

  forecastAt(location: string, atUtc: string | null): Forecast | null {
    const at = atUtc ? Date.parse(atUtc) : Infinity;
    for (let i = this.forecastLog.length - 1; i >= 0; i--) {
      const f = this.forecastLog[i];
      if (f.location === location && Date.parse(f.issued_utc) <= at) return f;
    }
    return null;
  }

  // ---- failure detection -------------------------------------------------------------

  /** A crashed controller misses this step's deadline: first "not responding", then crashed and adopted. */
  private detectAtTick(): void {
    for (const w of this.workers) {
      if (w.killedAtTick === null || w.status === 'dead') continue;
      w.missed += 1;
      if (w.missed === 1) {
        w.status = 'suspect';
        this.emit('worker_suspect', 'warn', `Controller ${w.worker_id} stopped responding. Its batteries get no new orders while the others cover for it.`, {
          worker_id: w.worker_id,
          pid: w.pid,
        });
      } else if (w.missed >= 2) {
        this.adopt(w);
      }
    }
  }

  private detectWhilePaused(nowMs: number): void {
    for (const w of this.workers) {
      if (w.killedAtMs === null || w.status === 'dead') continue;
      const age = nowMs - w.killedAtMs;
      if (w.status !== 'suspect' && age >= 2000) {
        w.status = 'suspect';
        w.missed = 1;
        this.emit('worker_suspect', 'warn', `Controller ${w.worker_id} stopped sending its heartbeat.`, { worker_id: w.worker_id, pid: w.pid });
      }
      if (age >= 6000) this.adopt(w);
    }
  }

  private adopt(dead: WorkerState): void {
    dead.status = 'dead';
    const survivors = this.workers.filter((w) => w.status === 'healthy');
    const moved: number[] = [];
    for (let g = 0; g < FLEET; g++) if (this.owner[g] === dead.worker_id) moved.push(g);
    moved.forEach((g, i) => {
      const to = survivors[i % survivors.length];
      this.owner[g] = to.worker_id;
      if (this.offlineWorkers.has(dead.worker_id)) this.offWorker[g] = 1;
    });
    for (const s of survivors) {
      if (!s.adopted_from.includes(dead.worker_id)) s.adopted_from.push(dead.worker_id);
    }
    const counts = new Map<number, number>();
    for (let g = 0; g < FLEET; g++) counts.set(this.owner[g], (counts.get(this.owner[g]) ?? 0) + 1);
    for (const w of this.workers) w.batteries = counts.get(w.worker_id) ?? 0;
    const ms = 1 + Math.round(this.random() * 3);
    this.emit('worker_dead', 'warn', `Controller ${dead.worker_id} crashed: it missed two 5-minute steps in a row.`, {
      worker_id: dead.worker_id,
      pid: dead.pid,
    });
    this.emit(
      'worker_adopted',
      'good',
      `Controller ${dead.worker_id}'s ${moved.length.toLocaleString('en-US')} batteries were handed to the other ${survivors.length} controllers in ${ms} ms.`,
      { worker_id: dead.worker_id, batteries: moved.length, adopted_by: survivors.map((s) => s.worker_id), adoption_ms: ms },
    );
  }

  // ---- full-year results (SIMULATED placeholders) -----------------------------------------

  startBacktest(): { job_id: string } {
    const id = crypto.randomUUID();
    this.jobs.set(id, { id, doneAtMs: this.now() + this.backtestMs, fail: this.backtestFails });
    return { job_id: id };
  }

  backtestStatus(jobId: string): { status: 'running' | 'done' | 'failed'; run_ids: string[]; error: string | null } {
    const job = this.jobs.get(jobId);
    if (!job) throw new MockError(404, 'unknown_job', 'No full-year run has that ID.');
    if (this.now() < job.doneAtMs) return { status: 'running', run_ids: [], error: null };
    if (job.fail) return { status: 'failed', run_ids: [], error: 'The practice server was told to fail this run (MOCK_BACKTEST_FAIL=1).' };
    this.resultsReady = true;
    return { status: 'done', run_ids: ['mock-bands', 'mock-forecast', 'mock-peak'], error: null };
  }

  results(): Results {
    if (!this.resultsReady) throw new MockError(404, 'no_results', 'No full-year results yet. Run the full year first.');
    const powerMw = NAMEPLATE_KW / 1000;
    const energyMwh = CAPACITY.reduce((a, b) => a + b, 0) / 1000;
    const avgKw = NAMEPLATE_KW / FLEET;
    const avgKwh = (energyMwh * 1000) / FLEET;
    const strategy = (key: string, label: string, perBattery: number, cycles: number, headline: boolean, days: number[]) => ({
      strategy: key,
      label,
      run_id: `mock-${key}`,
      is_headline: headline,
      per_battery_usd_year: perBattery,
      fleet_usd_year: Math.round(perBattery * FLEET),
      usd_per_kwh_year: Math.round((perBattery / avgKwh) * 100) / 100,
      usd_per_kw_year: Math.round((perBattery / avgKw) * 100) / 100,
      cycles_per_year: cycles,
      per_location_usd: {
        LZ_HOUSTON: Math.round(perBattery * PER_AREA * 1.04),
        LZ_NORTH: Math.round(perBattery * PER_AREA * 0.96),
      },
      top_days: TOP_DAY_DATES.map((date_local, i) => ({ date_local, usd: days[i] })),
    });
    return {
      year: 2025,
      generated_utc: new Date(this.now()).toISOString(),
      fleet_size: FLEET,
      fleet_power_mw: Math.round(powerMw * 10) / 10,
      fleet_energy_mwh: Math.round(energyMwh * 10) / 10,
      assumptions: [
        'SIMULATED: these are placeholder numbers from the practice (mock) server, not a real backtest.',
        'Gross wholesale energy value at ERCOT real-time prices (15-minute settlement).',
      ],
      strategies: [
        strategy('bands', 'Simple rule', 104.2, 286, false, [182_000, 121_500, 96_200, 88_900, 71_300, 64_800, 60_100, 55_700, 49_900, 47_200]),
        strategy('forecast', 'AI forecast (TimesFM-3)', 161.8, 301, true, [412_600, 318_400, 205_700, 164_900, 121_800, 98_300, 90_600, 84_100, 77_500, 70_900]),
        strategy('peak', 'Evening peak', 128.5, 365, false, [301_200, 226_000, 150_400, 118_800, 96_500, 80_200, 72_100, 66_400, 61_000, 58_300]),
      ],
      ceiling: { label: 'Best possible (perfect hindsight)', per_battery_usd_year: 251.0, fleet_usd_year: 251 * FLEET },
    };
  }
}

/** Simulated top days (dates chosen from known 2025 spike days; the dollar values are made up). */
const TOP_DAY_DATES = [
  '2025-04-07',
  '2025-05-16',
  '2025-01-15',
  '2025-08-19',
  '2025-06-24',
  '2025-02-20',
  '2025-07-29',
  '2025-09-03',
  '2025-05-13',
  '2025-08-05',
];

export function mockSettingsResponse() {
  return {
    defaults: DEFAULT_SETTINGS,
    editable: ['preset', 'speed_tps', 'strategy', 'start_charge_level'],
    presets: PRESETS,
    strategies: STRATEGIES,
    speeds: SPEEDS.map((v) => ({ value: v, label: `${v} steps per second` })),
  };
}
