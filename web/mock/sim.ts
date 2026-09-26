// SIMULATED fleet for the mock API (dev and tests only; never shipped).
//
// Real: the prices in ./data (ERCOT real-time prices of the four Texas Triangle
// load zones, each 2025 replay preset's day and the day after), and the full-year
// figures copied from RUNLOG.md (below). Simulated: everything else — batteries
// and where they sit, controllers, decisions, money, the race, forecasts and
// failures. The physics follow RUNLOG.md loosely (grid-side metering, sqrt(eta)
// each way, 20% reserve) so the site sees realistic shapes, but no live number
// here is a real result.
//
// 2026 (docs/API.md Year): real prices through Sep 20, then the AI's prediction.
// The mock has no 2026 data, so its "real" 2026 prices are 2025's from the same
// dates, a year on, and its prediction is simulated: for each 15 minutes, the
// median of the same time of day over the last 7 real days, smoothed, like a
// forecast's middle line (p50). Ticks after real_until_utc are `predicted`.
//
// Contract details (docs/API.md): worker_id 0-19 shown as "Controller N+1";
// battery ids b00000-b19999 in city blocks (Houston b00000-b06999, Dallas–Fort
// Worth b07000-b14999, San Antonio b15000-b17599, Austin b17600-b19999); the
// fleet split into 20 contiguous controller blocks of 1,000, as the backend's
// FleetLayout.default_owners does (controller 18, worker 17, spans San Antonio
// and Austin); idle message every 5 s; the race (a shadow fleet on the
// comparison strategy) in every snapshot and frame while a run exists.
//
// The failure model (docs/API.md, the human's rules): each worker reports the
// capacity-weighted charge of the batteries it owns (charge_level) and the
// usable share above the 20% reserve (usable_charge). A crash empties that
// controller's batteries to the reserve; the others take them over and run
// them; nothing comes back on its own: POST /workers/{id}/restart (or Reset)
// brings the controller back, taking its own block back with whatever charge
// those batteries have by then (worker_recovered). Taking a controller's
// batteries offline halves their usable charge. The race shadow mirrors both;
// the potential fleet (all online) never loses charge.
//
// The full-year numbers (GET /results/pitch per-battery figures and GET
// /results/ai-gains) are copied from the real backtest in RUNLOG.md (./gains.ts);
// the rest of the Results payload (cycles, top days, most city splits) is simulated.

import type {
  AiGains,
  Availability,
  AvailabilityRequest,
  AvailabilityResponse,
  BacktestStatus,
  BatteryInfo,
  EditableSettings,
  FailedCommand,
  FleetLayout,
  Forecast,
  FrameMessage,
  IdleMessage,
  LiveEvent,
  LocationTotals,
  Preset,
  Race,
  Results,
  RunStatus,
  Settings,
  Severity,
  SnapshotMessage,
  Tick,
  TickAction,
  Totals,
  Worker,
  YearInfo,
} from '../src/api/types.ts';
import { batteryRanges, DEFAULT_SHARES } from '../src/lib/fleet.ts';
import { CHARGING, DOWN, OFFLINE, packStatus, SELLING, WAITING } from '../src/map/status.ts';
import priceFixture from './data/ercot_rt_lz_preset_days_2025.json' with { type: 'json' };
import { MEASURED, mockAiGains, SPLIT_2026 } from './gains.ts';
import { buildMockLayout } from './layout.ts';

/** Present in every mock module so scripts/check-dist.mjs can prove none of it ships. */
export const MOCK_MARKER = 'FLEET_SIM_MOCK_DO_NOT_SHIP';
export const MOCK_VERSION =
  'mock-0.5 (simulated data; real ERCOT prices for the 2025 replay days; 2026 is 2025 a year on, then a simulated prediction; measured full-year figures from RUNLOG.md)';

/** The Texas Triangle load zones in config order. */
export const LOCS = ['LZ_HOUSTON', 'LZ_NORTH', 'LZ_CPS', 'LZ_AEN'] as const;
export type Loc = (typeof LOCS)[number];

const TICK_S = 300;
const INTERVAL_S = 900;
const DT_H = TICK_S / 3600;
const FLEET = 20_000;
const WORKERS = 20;
const SQRT_ETA = Math.sqrt(0.9);
const SOC_MIN = 0.2;
const SOC_MAX = 1.0;
const HISTORY_CAP = 2000;
const EVENTS_CAP = 50;
const FORECAST_EVERY_TICKS = 12;
const FORECAST_HORIZON_S = 4 * 3600;
const IDLE_EVERY_S = 5;
const TZ = 'America/Chicago';
/** docs/API.md: a spike is a 5-minute price at or above $1,000/MWh. */
const SPIKE_USD_MWH = 1000;

/** Same split as config.yaml / src/sim/fleet.location_counts: 7,000 / 8,000 / 2,600 / 2,400. */
const RANGES = batteryRanges(FLEET, DEFAULT_SHARES);
function rangeOf(loc: Loc) {
  const r = RANGES.find((x) => x.location === loc);
  if (!r) throw new Error(`no battery range for ${loc}`);
  return r;
}
export const COUNT = Object.fromEntries(LOCS.map((l) => [l, rangeOf(l).last - rangeOf(l).first + 1])) as Record<Loc, number>;
const BASE = Object.fromEntries(LOCS.map((l) => [l, rangeOf(l).first])) as Record<Loc, number>;
const LOC_OF = new Uint8Array(FLEET);
LOCS.forEach((loc, k) => LOC_OF.fill(k, BASE[loc], BASE[loc] + COUNT[loc]));

const perLoc = <T>(f: (loc: Loc) => T): Record<Loc, T> => Object.fromEntries(LOCS.map((l) => [l, f(l)])) as Record<Loc, T>;

/** docs/API.md: 2026's real prices end here (the end of the last real interval); the AI's prediction follows. */
export const REAL_UNTIL_2026 = '2026-09-21T05:00:00Z';

/** docs/API.md Year items. */
export const YEARS: YearInfo[] = [
  { year: 2025, label: '2025', description: 'Real ERCOT prices for the whole year.', real_until_utc: null },
  { year: 2026, label: '2026', description: 'Real prices through Sep 20, then the AI’s prediction.', real_until_utc: REAL_UNTIL_2026 },
];

/**
 * The 2025 presets mirror config.yaml presets.items (start_local added, as GET /settings sends it); each year
 * has a "From January 1" preset (docs/API.md). The mock loads two days per preset, so "From January 1" runs for
 * two days here.
 */
export const PRESETS: Preset[] = [
  {
    id: 'spike_evening',
    year: 2025,
    label: 'Price-spike evening',
    date_local: '2025-05-16',
    start_utc: '2025-05-16T21:00:00Z',
    start_local: '2025-05-16T16:00:00-05:00',
    description: 'All four cities jump above $2,000/MWh around 7:45 pm; San Antonio tops $3,000.',
  },
  {
    id: 'morning_spike',
    year: 2025,
    label: 'Biggest spike of 2025',
    date_local: '2025-04-07',
    start_utc: '2025-04-07T08:00:00Z',
    start_local: '2025-04-07T03:00:00-05:00',
    description: "The year's highest prices: all four cities pass $3,500/MWh just before 7 am.",
  },
  {
    id: 'summer_spike',
    year: 2025,
    label: 'Summer night spike',
    date_local: '2025-07-11',
    start_utc: '2025-07-11T23:00:00Z',
    start_local: '2025-07-11T18:00:00-05:00',
    description: 'All four cities jump to about $1,750/MWh at 9 pm.',
  },
  {
    id: 'ai_best_evening',
    year: 2025,
    label: "The AI's best evening",
    date_local: '2025-05-30',
    start_utc: '2025-05-30T23:00:00Z',
    start_local: '2025-05-30T18:00:00-05:00',
    description: 'The 2025 evening where the AI forecast pulled furthest ahead of the simple rule.',
  },
  {
    id: 'ordinary_day',
    year: 2025,
    label: 'An ordinary day',
    date_local: '2025-06-28',
    start_utc: '2025-06-28T05:00:00Z',
    start_local: '2025-06-28T00:00:00-05:00',
    description: 'A typical day in all four cities, with small ups and downs.',
  },
  {
    id: 'jan1_2025',
    year: 2025,
    label: 'From January 1',
    date_local: '2025-01-01',
    start_utc: '2025-01-01T06:00:00Z',
    start_local: '2025-01-01T00:00:00-06:00',
    description: 'The year from its first hour (the practice server loads two days).',
  },
  {
    id: 'predicted_2026',
    year: 2026,
    label: 'Where real prices end',
    date_local: '2026-09-20',
    start_utc: '2026-09-20T23:00:00Z',
    start_local: '2026-09-20T18:00:00-05:00',
    description: 'The last real evening, Sep 20; from midnight on, the prices are the AI’s prediction.',
  },
  {
    id: 'jan1_2026',
    year: 2026,
    label: 'From January 1',
    date_local: '2026-01-01',
    start_utc: '2026-01-01T06:00:00Z',
    start_local: '2026-01-01T00:00:00-06:00',
    description: 'The year from its first hour (the practice server loads two days).',
  },
];

/** config.yaml presets.default is ai_best_evening. */
export const DEFAULT_SETTINGS: Settings = {
  year: 2025,
  preset: 'ai_best_evening',
  start_utc: '2025-05-30T23:00:00Z',
  speed_tps: 2,
  strategy: 'forecast',
  start_charge_level: 0.9,
  fleet_size: FLEET,
  power_kw_range: [5.0, 11.5],
  capacity_kwh_range: [10.0, 20.0],
  locations: { ...DEFAULT_SHARES },
};

export const SPEEDS = [1, 2, 5, 10];
export const STRATEGIES = [
  {
    id: 'bands',
    label: 'Simple rule',
    description: 'Buys in the cheapest quarter of the past week and sells in the most expensive tenth.',
  },
  { id: 'forecast', label: 'AI forecast (TimesFM-3)', description: 'Saves its charge for the hours a price forecast expects to be most expensive.' },
  { id: 'peak', label: 'Evening peak', description: 'Charges from 10 am to 3 pm and sells from 6 pm to 9 pm.' },
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

// ---- prices: real 2025, a stand-in for 2026, and the simulated prediction -----------------

type PriceRow = { ts_utc: string; ts_local: string } & Record<Loc, number>;

interface Segment {
  preset: string;
  startS: number;
  endS: number;
  cheap: Record<Loc, number>;
  sellFloor: Record<Loc, number>;
}

const toS = (iso: string) => Math.round(Date.parse(iso) / 1000);

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

const DAY_S = 86_400;
/** 2025-09-20 + 365 days is 2026-09-20 (same weekday count aside): the stand-in shift. */
const YEAR_S = 365 * DAY_S;
export const REAL_UNTIL_2026_S = toS(REAL_UNTIL_2026);
/** How much of the prediction the mock simulates, and from how many real days before it. */
const PREDICTION_DAYS = 2;
const PREDICTION_LOOKBACK_DAYS = 7;
/** Fixture segments that stand in for 2026's real prices, a year on (see export_prices.py MOCK_ONLY). */
const STAND_IN_SEGMENTS = new Set(['jan1_2025', 'stand_in_2026_sep']);

/** Every 15-minute interval the mock has a price for, by its start (UTC seconds). */
const BOOK = new Map<number, { row: PriceRow; predicted: boolean }>();

for (const seg of priceFixture.segments) {
  for (const r of seg.rows as PriceRow[]) {
    BOOK.set(toS(r.ts_utc), { row: r, predicted: false });
    // 2026's "real" prices on the practice server: 2025's, a year on (same local clock and UTC offset).
    const later = toS(r.ts_utc) + YEAR_S;
    if (STAND_IN_SEGMENTS.has(seg.preset) && later < REAL_UNTIL_2026_S) {
      BOOK.set(later, {
        row: { ...r, ts_utc: isoUtc(later), ts_local: r.ts_local.replace(/^2025/, '2026') },
        predicted: false,
      });
    }
  }
}

/**
 * The simulated prediction after REAL_UNTIL_2026: for each 15 minutes, the median of the same time of day over
 * the last 7 real days, pulled a little towards their mean and smoothed over an hour or so, like a forecast's
 * middle line. Plausible practice data, not TimesFM.
 */
function simulatePrediction(): void {
  const steps = (PREDICTION_DAYS * DAY_S) / INTERVAL_S;
  const raw: Record<Loc, number[]> = perLoc(() => []);
  for (let k = 0; k < steps; k++) {
    const s = REAL_UNTIL_2026_S + k * INTERVAL_S;
    for (const loc of LOCS) {
      const past: number[] = [];
      for (let d = 1; past.length < PREDICTION_LOOKBACK_DAYS && d <= PREDICTION_LOOKBACK_DAYS + PREDICTION_DAYS; d++) {
        const back = s - d * DAY_S;
        const got = back < REAL_UNTIL_2026_S ? BOOK.get(back) : undefined;
        if (got) past.push(got.row[loc]);
      }
      const mean = past.reduce((a, b) => a + b, 0) / Math.max(1, past.length);
      raw[loc].push(past.length ? mean + (percentile(past, 0.5) - mean) * 0.85 : 30);
    }
  }
  for (let k = 0; k < steps; k++) {
    const s = REAL_UNTIL_2026_S + k * INTERVAL_S;
    const row = { ts_utc: isoUtc(s), ts_local: '' } as PriceRow;
    for (const loc of LOCS) {
      const lo = Math.max(0, k - 2);
      const hi = Math.min(steps - 1, k + 2);
      let sum = 0;
      for (let j = lo; j <= hi; j++) sum += raw[loc][j];
      row[loc] = round(sum / (hi - lo + 1), 2);
    }
    // Late September 2026 is on Central daylight time (UTC-5).
    row.ts_local = `${new Date((s - 5 * 3600) * 1000).toISOString().slice(0, 19)}-05:00`;
    BOOK.set(s, { row, predicted: true });
  }
}
simulatePrediction();

function rowAt(tsS: number): PriceRow | null {
  return BOOK.get(Math.floor(tsS / INTERVAL_S) * INTERVAL_S)?.row ?? null;
}

/** 15-minute price, forward-filled within its own interval only; null where no data was loaded. */
export function priceAt(loc: Loc, tsS: number): number | null {
  const row = rowAt(tsS);
  return row && typeof row[loc] === 'number' ? row[loc] : null;
}

/** Is this moment's price the (simulated) AI prediction rather than a real one? */
export function isPredicted(tsS: number): boolean {
  return BOOK.get(Math.floor(tsS / INTERVAL_S) * INTERVAL_S)?.predicted ?? false;
}

/** A preset's two loaded days, from its local midnight; null when the mock has no prices for all of them. */
function segmentFor(preset: Preset): Segment | null {
  const offset = preset.start_local?.slice(-6) ?? '-05:00';
  const startS = toS(`${preset.date_local}T00:00:00${offset}`);
  const endS = startS + 2 * DAY_S;
  const rows: PriceRow[] = [];
  for (let s = startS; s < endS; s += INTERVAL_S) {
    const got = BOOK.get(s);
    if (!got) return null;
    rows.push(got.row);
  }
  const of = (loc: Loc) => rows.map((r) => r[loc]);
  return {
    preset: preset.id,
    startS,
    endS,
    cheap: perLoc((l) => percentile(of(l), 0.3)),
    sellFloor: perLoc((l) => percentile(of(l), 0.85)),
  };
}

function isoUtc(tsS: number): string {
  return new Date(tsS * 1000).toISOString().replace('.000Z', 'Z');
}

function isoLocal(tsS: number): string {
  const row = rowAt(tsS);
  const offset = row ? row.ts_local.slice(-6) : '-05:00'; // e.g. -05:00
  const sign = offset.startsWith('-') ? -1 : 1;
  const [h, m] = offset.slice(1).split(':').map(Number);
  const local = new Date((tsS + sign * (h * 3600 + m * 60)) * 1000).toISOString().slice(0, 19);
  return `${local}${offset}`;
}

function localHour(tsS: number): number {
  const local = isoLocal(tsS);
  return Number(local.slice(11, 13)) + Number(local.slice(14, 16)) / 60;
}

function spokenDate(tsS: number): string {
  const d = new Date(tsS * 1000);
  const date = d.toLocaleDateString('en-US', { timeZone: TZ, month: 'long', day: 'numeric', year: 'numeric' });
  const time = d
    .toLocaleTimeString('en-US', { timeZone: TZ, hour: 'numeric', minute: '2-digit' })
    .replace(':00', '')
    .toLowerCase();
  return `${date}, from ${time}`;
}

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
const PERMUTATION = perLoc((l) => shuffled(COUNT[l], 7 + LOCS.indexOf(l)));
const NAMEPLATE_KW = POWER.reduce((a, b) => a + b, 0);
const CAPACITY_BY_LOC = perLoc((l) => CAPACITY.subarray(BASE[l], BASE[l] + COUNT[l]).reduce((a, b) => a + b, 0));

const LAYOUT = buildMockLayout(FLEET, DEFAULT_SHARES);

const areaOf = (g: number): Loc => LOCS[LOC_OF[g]];
export const batteryId = (g: number) => `b${String(g).padStart(5, '0')}`;

export function batteryIndex(id: string): number | null {
  const m = /^b(\d{5})$/.exec(id);
  if (!m) return null;
  const n = Number(m[1]);
  return n < FLEET ? n : null;
}

/**
 * Worker `w`'s own block of battery indices [start, end), as the backend's FleetLayout.default_owners: the
 * fleet in id order split into 20 contiguous chunks (numpy.array_split), so each controller serves one city,
 * or two where a city boundary falls inside its chunk (worker 17: 600 San Antonio + 400 Austin).
 */
function defaultBlock(w: number): [number, number] {
  const small = Math.floor(FLEET / WORKERS);
  const extra = FLEET % WORKERS;
  const start = w * small + Math.min(w, extra);
  return [start, start + small + (w < extra ? 1 : 0)];
}

/** Initial cohorts: every worker on its own block. */
function defaultOwners(out: Uint8Array): void {
  for (let w = 0; w < WORKERS; w++) out.fill(w, ...defaultBlock(w));
}

/** Usable energy above the 20% reserve, 0-1, for a charge level (docs/API.md Worker.usable_charge). */
const usableOf = (level: number) => Math.min(1, Math.max(0, (level - SOC_MIN) / (SOC_MAX - SOC_MIN)));

/** Halve the usable energy of a battery at charge `soc` (a controller's batteries taken offline). */
const halvedSoc = (soc: number) => SOC_MIN + (soc - SOC_MIN) / 2;

/** {location: batteries} among these battery indices, only locations that have some (event details). */
function byLocation(idx: Iterable<number>): Record<string, number> {
  const counts = new Uint32Array(LOCS.length);
  for (const g of idx) counts[LOC_OF[g]]++;
  const out: Record<string, number> = {};
  LOCS.forEach((loc, k) => {
    if (counts[k]) out[loc] = counts[k];
  });
  return out;
}

/** People count controllers from 1; worker_id counts from 0. */
const controller = (workerId: number) => `Controller ${workerId + 1}`;
const workerDetail = (w: { worker_id: number; pid: number | null }) => ({
  worker_id: w.worker_id,
  controller: w.worker_id + 1,
  pid: w.pid,
});

const CITY_NAMES: Record<Loc, string> = {
  LZ_HOUSTON: 'Houston',
  LZ_NORTH: 'Dallas–Fort Worth',
  LZ_CPS: 'San Antonio',
  LZ_AEN: 'Austin',
};
const AREA_NAMES = perLoc((l) => `${CITY_NAMES[l]} area`);

/** "Houston", "Houston and Austin", "Houston, Austin and San Antonio". */
function cityList(locs: Loc[]): string {
  const names = locs.map((l) => CITY_NAMES[l]);
  if (locs.length === LOCS.length) return 'all four cities';
  return names.length <= 1 ? (names[0] ?? '') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

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
  /** POST /workers/{id}/restart was called at this time; it takes its batteries back at the next step. */
  restartAtMs: number | null;
}

/** Killed or not responding, and not handed over yet: its batteries are "controller down". */
const isCrashing = (w: WorkerState) => w.status === 'suspect' || (w.status !== 'dead' && w.killedAtMs !== null);

interface Job {
  id: string;
  doneAtMs: number;
  fail: boolean;
  /** Stopped with POST /backtest/{job_id}/cancel. */
  cancelled: boolean;
  /** The year it re-runs (POST /backtest {"year"}). */
  year: number;
}

/** The years with full-year results (docs/API.md GET /results/pitch?year=): 2025 is the pitch year. */
export const RESULT_YEARS = [2025, 2026];
const PITCH_YEAR = 2025;

export interface MockSimOptions {
  now?: () => number;
  random?: () => number;
  backtestMs?: number;
  backtestFails?: boolean;
  /** false: every year answers 404 no_results until a full-year re-run of it finishes. */
  resultsBeforeBacktest?: boolean;
  /** These years answer 404 no_results until a re-run of them finishes (MOCK_NO_RESULTS=2026). */
  noResultsYears?: number[];
  /** GET /results/ai-gains always answers 404 no_results (a backend without them yet). */
  noAiGains?: boolean;
}

const zeroLocTotals = (): Record<Loc, LocationTotals> => perLoc(() => ({ revenue_usd: 0, sold_kwh: 0, bought_kwh: 0 }));
const zeroTotals = (): Totals => ({ revenue_usd: 0, sold_kwh: 0, bought_kwh: 0, ticks: 0 });

// ---- decisions (simulated strategies) ------------------------------------------------

function decide(strategy: string, seg: Segment | null, loc: Loc, tsS: number, decisionPrice: number): TickAction {
  if (strategy === 'peak') {
    const h = localHour(tsS);
    if (h >= 10 && h < 15) return 'charge';
    if (h >= 18 && h < 21) return 'sell';
    return 'hold';
  }
  if (!seg) return 'hold';
  if (strategy === 'bands') {
    const past: number[] = [];
    for (let s = seg.startS; s < tsS - INTERVAL_S; s += INTERVAL_S) {
      const p = priceAt(loc, s);
      if (p !== null) past.push(p);
    }
    if (past.length < 12) return 'hold';
    if (decisionPrice <= percentile(past, 0.25)) return 'charge';
    if (decisionPrice >= percentile(past, 0.9) && decisionPrice > 0) return 'sell';
    return 'hold';
  }
  // "forecast": peeks at the loaded prices from now to 4 hours ahead, standing in for a good forecast, so it
  // sells into a spike as it happens rather than 15 minutes late.
  const expected = priceAt(loc, tsS) ?? decisionPrice;
  let ahead = expected;
  for (let s = tsS; s < tsS + FORECAST_HORIZON_S; s += INTERVAL_S) {
    const p = priceAt(loc, s);
    if (p !== null && p > ahead) ahead = p;
  }
  if (expected >= seg.sellFloor[loc] && expected >= 0.45 * ahead) return 'sell';
  if (expected <= seg.cheap[loc] && ahead < 3 * seg.cheap[loc] + 60) return 'charge';
  return 'hold';
}

const strategyLabel = (id: string) => STRATEGIES.find((s) => s.id === id)?.label ?? id;

/** How the race_update messages name a method: "the AI", "the simple rule". */
const SPOKEN: Record<string, string> = { forecast: 'AI', bands: 'simple rule', peak: 'evening peak' };
const spoken = (id: string) => SPOKEN[id] ?? id;

// ---- the race (docs/API.md Race) ---------------------------------------------------------

/**
 * A shadow copy of the fleet running the comparison strategy side by side with the live one: the same
 * batteries and starting charge, the same prices at the same decision lag, the same availability. It has
 * no controllers, so no crashes and no failed commands, and no delivery noise.
 */
class RaceFleet {
  readonly strategy: string;
  readonly label: string;
  revenue = 0;
  fleetLevel: number;
  private readonly soc = new Float64Array(FLEET);
  private readonly revenueBy = perLoc(() => 0);
  private readonly action = perLoc<TickAction>(() => 'hold');
  private readonly level: Record<Loc, number>;

  constructor(strategy: string, startCharge: number) {
    this.strategy = strategy;
    this.label = strategyLabel(strategy);
    this.soc.fill(startCharge);
    this.level = perLoc(() => startCharge);
    this.fleetLevel = startCharge;
  }

  /** One 5-minute step on the live fleet's prices and availability. */
  step(
    seg: Segment | null,
    tsS: number,
    price: Record<Loc, number>,
    decision: Record<Loc, number>,
    available: (g: number) => boolean,
  ): void {
    let stored = 0;
    let capacity = 0;
    for (const loc of LOCS) {
      let want = decide(this.strategy, seg, loc, tsS, decision[loc]);
      const base = BASE[loc];
      const count = COUNT[loc];
      const h = new Float64Array(count);
      let headroom = 0;
      if (want !== 'hold') {
        for (let n = 0; n < count; n++) {
          const g = base + n;
          if (!available(g)) continue;
          const energy =
            want === 'sell'
              ? ((this.soc[g] - SOC_MIN) * CAPACITY[g] * SQRT_ETA) / DT_H
              : ((SOC_MAX - this.soc[g]) * CAPACITY[g]) / SQRT_ETA / DT_H;
          h[n] = Math.max(0, Math.min(POWER[g], energy));
          headroom += h[n];
        }
      }
      if (headroom < 1) want = 'hold';
      const sign = want === 'sell' ? 1 : want === 'charge' ? -1 : 0;
      let delivered = 0;
      let locStored = 0;
      for (let n = 0; n < count; n++) {
        const g = base + n;
        const kw = sign * h[n];
        if (kw > 0) this.soc[g] -= (kw * DT_H) / SQRT_ETA / CAPACITY[g];
        else if (kw < 0) this.soc[g] += (-kw * DT_H * SQRT_ETA) / CAPACITY[g];
        this.soc[g] = Math.min(SOC_MAX, Math.max(SOC_MIN, this.soc[g]));
        delivered += kw;
        locStored += this.soc[g] * CAPACITY[g];
      }
      const earned = ((delivered * DT_H) / 1000) * price[loc];
      this.revenueBy[loc] += earned;
      this.revenue += earned;
      this.action[loc] = want;
      this.level[loc] = locStored / CAPACITY_BY_LOC[loc];
      stored += locStored;
      capacity += CAPACITY_BY_LOC[loc];
    }
    this.fleetLevel = stored / capacity;
  }

  revenueOf(loc: Loc): number {
    return this.revenueBy[loc];
  }

  /** The same batteries as the live fleet's crashed controller, emptied to the reserve (docs/API.md Mirroring). */
  empty(idx: Iterable<number>): void {
    for (const g of idx) this.soc[g] = SOC_MIN;
    this.recount();
  }

  /** The same batteries as the live fleet's controller taken offline, their usable charge halved. */
  halve(idx: Iterable<number>): void {
    for (const g of idx) this.soc[g] = halvedSoc(this.soc[g]);
    this.recount();
  }

  private recount(): void {
    let stored = 0;
    let capacity = 0;
    for (const loc of LOCS) {
      let locStored = 0;
      for (let n = 0; n < COUNT[loc]; n++) locStored += this.soc[BASE[loc] + n] * CAPACITY[BASE[loc] + n];
      this.level[loc] = locStored / CAPACITY_BY_LOC[loc];
      stored += locStored;
      capacity += CAPACITY_BY_LOC[loc];
    }
    this.fleetLevel = stored / capacity;
  }

  toJson(liveRevenue: number): Race {
    return {
      strategy: this.strategy,
      label: this.label,
      revenue_usd: round(this.revenue, 4),
      by_location: perLoc((l) => ({
        revenue_usd: round(this.revenueBy[l], 4),
        charge_level: round(this.level[l], 4),
        action: this.action[l],
      })),
      charge_level: round(this.fleetLevel, 4),
      live_minus_race_usd: round(liveRevenue - this.revenue, 4),
    };
  }
}

export class MockSim {
  status: RunStatus = 'idle';
  runId: string | null = null;
  settings: Settings = { ...DEFAULT_SETTINGS };

  private readonly now: () => number;
  private readonly random: () => number;
  private readonly backtestMs: number;
  private readonly backtestFails: boolean;
  private readonly noAiGains: boolean;
  /** Years that answer 404 no_results until a full-year re-run of them finishes. */
  private readonly missing: Set<number>;

  private segment: Segment | null = null;
  private startedAtMs = 0;
  private tickIndex = 0;
  private history: Tick[] = [];
  private totals: Totals = zeroTotals();
  private totalsBy: Record<Loc, LocationTotals> = zeroLocTotals();
  private lastRun: { run_id: string; totals: Totals } | null = null;
  private idleSeconds = 0;
  private soc = new Float64Array(FLEET);
  private owner = new Uint8Array(FLEET);
  private offLocation = new Uint8Array(FLEET);
  private offWorker = new Uint8Array(FLEET);
  private offBattery = new Uint8Array(FLEET);
  private lastSetpoint = new Float64Array(FLEET);
  /** Each battery's code at the latest tick (docs/API.md battery_status), or null before the first tick. */
  private tickCodes: Uint8Array | null = null;
  private workers: WorkerState[] = [];
  private offlineWorkers = new Set<number>();
  private failed: { count: number; recent: FailedCommand[] } = { count: 0, recent: [] };
  private events: LiveEvent[] = [];
  private pendingEvents: LiveEvent[] = [];
  private pendingTicks: Tick[] = [];
  private forecast: Partial<Record<Loc, Forecast>> = {};
  private forecastLog: Forecast[] = [];
  private pendingForecast: Partial<Record<Loc, Forecast>> | null = null;
  private lastAction: Record<Loc, TickAction> = perLoc(() => 'hold');
  private spiked: Record<Loc, boolean> = perLoc(() => false);
  private race = new RaceFleet('bands', DEFAULT_SETTINGS.start_charge_level);
  /** docs/API.md race.potential: the live strategy with every battery online and no crashes. */
  private potential = new RaceFleet(DEFAULT_SETTINGS.strategy, DEFAULT_SETTINGS.start_charge_level);
  /** Some battery went offline or some controller crashed in this run: race.shows_potential. */
  private hadOutage = false;
  /** The prediction_started event went out (at the first predicted tick). */
  private predictionNoted = false;
  /** A spike is on in at least one city (for the race_update events). */
  private inSpike = false;
  private eventSeq = 0;
  private pidSeq = 4100;
  private jobs = new Map<string, Job>();
  private runningJob: string | null = null;
  private currentTickUtc: string | null = null;

  constructor(options: MockSimOptions = {}) {
    this.now = options.now ?? Date.now;
    this.random = options.random ?? mulberry32(1234);
    this.backtestMs = options.backtestMs ?? 6000;
    this.backtestFails = options.backtestFails ?? false;
    this.noAiGains = options.noAiGains ?? false;
    this.missing = new Set(options.resultsBeforeBacktest === false ? RESULT_YEARS : (options.noResultsYears ?? []));
    this.resetFleet();
  }

  // ---- lifecycle ---------------------------------------------------------------

  private resetFleet(startCharge = DEFAULT_SETTINGS.start_charge_level): void {
    this.soc.fill(startCharge);
    defaultOwners(this.owner);
    this.offLocation.fill(0);
    this.offWorker.fill(0);
    this.offBattery.fill(0);
    this.lastSetpoint.fill(0);
    this.tickCodes = null;
    this.offlineWorkers.clear();
    this.workers = Array.from({ length: WORKERS }, (_, i) => ({
      worker_id: i,
      status: 'healthy' as const,
      pid: this.pidSeq++,
      batteries: 0,
      last_tick_utc: null,
      adopted_from: [],
      killedAtTick: null,
      killedAtMs: null,
      missed: 0,
      restartAtMs: null,
    }));
    this.recountWorkers();
  }

  private rememberRun(): void {
    if (this.runId) this.lastRun = { run_id: this.runId, totals: this.publicTotals() };
  }

  start(partial: Partial<EditableSettings>): { run_id: string; settings: Settings } {
    const next: Settings = { ...DEFAULT_SETTINGS };
    const year = partial.year === undefined || partial.year === null ? null : Number(partial.year);
    if (year !== null && !YEARS.some((y) => y.year === year)) {
      throw new MockError(400, 'invalid_year', 'The year must be 2025 or 2026.');
    }
    // A preset belongs to one year; a year alone gets the default preset, or that year's first.
    const yearDefault =
      year === null || year === DEFAULT_SETTINGS.year ? DEFAULT_SETTINGS.preset : PRESETS.find((p) => p.year === year)?.id;
    const presetId = partial.preset ?? yearDefault ?? DEFAULT_SETTINGS.preset;
    const preset = PRESETS.find((p) => p.id === presetId);
    if (!preset) throw new MockError(400, 'unknown_preset', `There is no replay called "${presetId}".`);
    if (year !== null && preset.year !== year) {
      throw new MockError(400, 'invalid_preset', `"${preset.label}" is a ${preset.year} replay. Pick one of ${year}'s.`);
    }
    const segment = segmentFor(preset);
    if (!segment) {
      throw new MockError(501, 'not_in_mock', `The practice server has no prices loaded for "${preset.label}".`);
    }
    next.year = preset.year;
    next.preset = preset.id;
    next.start_utc = preset.start_utc;
    if (partial.speed_tps !== undefined) {
      if (!SPEEDS.includes(Number(partial.speed_tps))) {
        throw new MockError(400, 'bad_speed', 'Speed must be 1, 2, 5 or 10 steps per second.');
      }
      next.speed_tps = Number(partial.speed_tps);
    }
    if (partial.strategy !== undefined) {
      if (!STRATEGIES.some((s) => s.id === partial.strategy)) {
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
    this.rememberRun();
    this.settings = next;
    this.segment = segment;
    this.runId = crypto.randomUUID();
    this.status = 'starting';
    this.startedAtMs = this.now();
    this.tickIndex = 0;
    this.history = [];
    this.pendingTicks = [];
    this.totals = zeroTotals();
    this.totalsBy = zeroLocTotals();
    this.failed = { count: 0, recent: [] };
    this.events = [];
    this.pendingEvents = [];
    this.forecast = {};
    this.forecastLog = [];
    this.pendingForecast = null;
    this.lastAction = perLoc(() => 'hold');
    this.spiked = perLoc(() => false);
    this.inSpike = false;
    this.hadOutage = false;
    this.predictionNoted = false;
    this.resetFleet(next.start_charge_level);
    // The comparison is the simple rule, or the AI forecast when the live run uses the simple rule.
    this.race = new RaceFleet(next.strategy === 'bands' ? 'forecast' : 'bands', next.start_charge_level);
    this.potential = new RaceFleet(next.strategy, next.start_charge_level);
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
      this.emit('speed_changed', 'info', `Speed changed: 1 hour of market time now takes ${12 / speed} seconds.`, {
        speed_tps: speed,
      });
    }
    return { speed_tps: speed };
  }

  reset(): { status: RunStatus } {
    this.rememberRun();
    this.status = 'idle';
    this.runId = null;
    this.segment = null;
    this.history = [];
    this.pendingTicks = [];
    this.totals = zeroTotals();
    this.totalsBy = zeroLocTotals();
    this.failed = { count: 0, recent: [] };
    this.forecast = {};
    this.pendingForecast = null;
    this.events = [];
    this.pendingEvents = [];
    this.idleSeconds = IDLE_EVERY_S - 1; // the next wall second announces "idle" at once
    this.resetFleet();
    return { status: 'idle' };
  }

  // ---- the map ------------------------------------------------------------------

  /** GET /fleet/layout: static per fleet. */
  layout(): FleetLayout {
    return LAYOUT.json;
  }

  /**
   * battery_status codes: the latest tick's, like the backend (so, like the backend, a frame at
   * 5-10 ticks a second can miss the two ticks a crashed controller's batteries are down). As
   * docs/API.md asks, an availability change or a crash shows within one frame, even while paused:
   * offline batteries show at once, and while paused a crashed controller's batteries show as down
   * from the moment it is killed until they are handed over.
   */
  statusCodes(): Uint8Array {
    const codes = this.status === 'idle' || !this.tickCodes ? new Uint8Array(FLEET).fill(WAITING) : this.tickCodes.slice();
    const down = this.status === 'paused' ? new Set(this.workers.filter(isCrashing).map((w) => w.worker_id)) : null;
    for (let g = 0; g < FLEET; g++) {
      if (!this.isAvailable(g)) codes[g] = OFFLINE;
      else if (down?.has(this.owner[g])) codes[g] = DOWN;
      else if (codes[g] === OFFLINE) codes[g] = WAITING; // back online since the last tick
    }
    return codes;
  }

  batteryStatus(): string {
    return Buffer.from(packStatus(this.statusCodes())).toString('base64');
  }

  // ---- availability ------------------------------------------------------------

  private isAvailable(g: number): boolean {
    return !this.offLocation[g] && !this.offWorker[g] && !this.offBattery[g];
  }

  private countAvailable(): { total: number; byLoc: Record<Loc, number> } {
    const byLoc = perLoc(() => 0);
    for (let g = 0; g < FLEET; g++) if (this.isAvailable(g)) byLoc[areaOf(g)]++;
    return { total: LOCS.reduce((s, l) => s + byLoc[l], 0), byLoc };
  }

  availability(): Availability {
    const { total, byLoc } = this.countAvailable();
    return {
      available: total,
      total: FLEET,
      by_location: perLoc((l) => ({ available: byLoc[l], total: COUNT[l] })),
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
        if (!LOCS.includes(loc)) throw new MockError(400, 'bad_location', `Unknown area. Use one of ${LOCS.join(', ')}.`);
        if (!(f >= 0 && f <= 1)) throw new MockError(400, 'bad_fraction', 'The offline share must be between 0 and 1.');
        const base = BASE[loc];
        this.offLocation.fill(0, base, base + COUNT[loc]);
        affected = Math.round(f * COUNT[loc]);
        const perm = PERMUTATION[loc];
        for (let n = 0; n < affected; n++) this.offLocation[base + perm[n]] = 1;
        message =
          affected === 0
            ? `All ${AREA_NAMES[loc]} batteries are available again.`
            : `${affected.toLocaleString('en-US')} ${CITY_NAMES[loc]}-area batteries were taken offline, so the fleet's plan shrinks.`;
        break;
      }
      case 'worker': {
        const w = this.workers.find((x) => x.worker_id === Number(req.worker_id));
        if (!w) throw new MockError(404, 'unknown_worker', `There is no worker ${req.worker_id}.`);
        const offline = req.available === false;
        const owned = this.ownedBy(w.worker_id);
        // docs/API.md failure model: going offline halves its batteries' usable charge, at that moment (the
        // race shadow too); while offline they don't move; back online they carry on from there.
        if (offline && !this.offlineWorkers.has(w.worker_id)) {
          for (const g of owned) this.soc[g] = halvedSoc(this.soc[g]);
          this.race.halve(owned);
        }
        for (const g of owned) this.offWorker[g] = offline ? 1 : 0;
        affected = owned.length;
        if (offline) this.offlineWorkers.add(w.worker_id);
        else this.offlineWorkers.delete(w.worker_id);
        message = offline
          ? `${controller(w.worker_id)}'s ${affected.toLocaleString('en-US')} batteries were taken offline, and half their usable charge is lost. The controller itself keeps running.`
          : `${controller(w.worker_id)}'s batteries are back online.`;
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
        message = 'Every battery is back online.';
        break;
      }
      default:
        throw new MockError(400, 'bad_scope', 'scope must be location, worker, battery or all.');
    }
    const { total } = this.countAvailable();
    if (total < FLEET && this.status !== 'idle') this.hadOutage = true; // race.shows_potential, for the rest of the run
    if (this.status !== 'idle') this.emit('availability_changed', 'info', message, { request: req, affected, available: total });
    return { applied_from_tick_utc: isoUtc(this.nextTickS()), affected, available: total, total: FLEET };
  }

  battery(id: string): BatteryInfo {
    const g = batteryIndex(id);
    if (g === null) throw new MockError(404, 'unknown_battery', `No battery has the ID ${id}.`);
    const loc = areaOf(g);
    return {
      battery_id: batteryId(g),
      location: loc,
      city: CITY_NAMES[loc],
      lonlat: [LAYOUT.lonlat[2 * g], LAYOUT.lonlat[2 * g + 1]],
      worker_id: this.owner[g],
      capacity_kwh: round(CAPACITY[g]),
      power_kw: round(POWER[g]),
      charge_level: round(this.soc[g], 3),
      power_kw_now: round(this.lastSetpoint[g]),
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
      if (!target) throw new MockError(404, 'unknown_worker', `There is no worker ${workerId} (ids run 0 to 19).`);
      if (target.status === 'dead' || target.killedAtTick !== null || target.killedAtMs !== null) {
        throw new MockError(409, 'already_dead', `${controller(workerId)} has already crashed.`);
      }
    }
    if (alive.length - 1 < 2) {
      throw new MockError(409, 'too_few_healthy', 'At least 2 controllers must keep working. Press Reset to bring the others back.');
    }
    target.killedAtTick = this.tickIndex;
    target.killedAtMs = this.now();
    target.restartAtMs = null; // crashed again while restarting: it stays down
    // docs/API.md failure model: a crash empties its batteries to the reserve (the race shadow's too); the
    // others take them over as they are.
    const emptied = this.ownedBy(target.worker_id);
    for (const g of emptied) this.soc[g] = SOC_MIN;
    this.race.empty(emptied);
    this.hadOutage = true; // race.shows_potential, for the rest of the run
    return { worker_id: target.worker_id, pid: target.pid ?? 0, killed_at_utc: new Date(this.now()).toISOString() };
  }

  /**
   * POST /workers/{worker_id}/restart: a crashed controller comes back only this way (or with Reset). It is
   * "starting" until the next step (a second while paused), then takes its own block back with whatever charge
   * those batteries have by then, and says so (worker_recovered).
   */
  restart(workerId: number): { worker_id: number; status: 'starting' } {
    const w = Number.isInteger(workerId) ? this.workers.find((x) => x.worker_id === workerId) : undefined;
    if (!w) throw new MockError(404, 'unknown_worker', `There is no worker ${workerId} (ids run 0 to 19).`);
    if (w.status !== 'dead' || w.restartAtMs !== null) {
      throw new MockError(409, 'not_crashed', `${controller(workerId)} has not crashed, so there is nothing to restart.`);
    }
    Object.assign(w, { status: 'starting', pid: this.pidSeq++, restartAtMs: this.now(), killedAtTick: null, killedAtMs: null, missed: 0 });
    return { worker_id: workerId, status: 'starting' };
  }

  /** A restarted controller takes its own block back (see restart). */
  private recover(w: WorkerState): void {
    const [start, end] = defaultBlock(w.worker_id);
    let stored = 0;
    let capacity = 0;
    const block: number[] = [];
    for (let g = start; g < end; g++) {
      this.owner[g] = w.worker_id;
      stored += this.soc[g] * CAPACITY[g];
      capacity += CAPACITY[g];
      block.push(g);
    }
    Object.assign(w, { status: 'healthy', restartAtMs: null, adopted_from: [] });
    for (const other of this.workers) other.adopted_from = other.adopted_from.filter((id) => id !== w.worker_id);
    this.recountWorkers();
    const usable = Math.round(usableOf(stored / capacity) * 100);
    this.emit(
      'worker_recovered',
      'good',
      `${controller(w.worker_id)} is back and has taken back its ${block.length.toLocaleString('en-US')} batteries${this.inCities(block)}, at ${usable}% usable charge.`,
      { ...workerDetail(w), batteries: block.length, by_location: byLocation(block) },
    );
  }

  /** Restarts waiting for their step: at a tick, or after a second while nothing ticks (paused, finished). */
  private recoverPending(nowMs: number | null): void {
    for (const w of this.workers) {
      if (w.status === 'starting' && w.restartAtMs !== null && (nowMs === null || nowMs - w.restartAtMs >= 1000)) this.recover(w);
    }
  }

  // ---- the clock -------------------------------------------------------------------

  private tickS(index: number): number {
    return toS(this.settings.start_utc) + index * TICK_S;
  }

  private nextTickS(): number {
    return this.tickS(this.tickIndex);
  }

  simTimeUtc(): string {
    const last = this.history[this.history.length - 1];
    return last ? last.ts_utc : this.settings.start_utc;
  }

  /**
   * Called once per wall-clock second. Returns the frame to broadcast while a run exists,
   * the idle message every 5 s when none does, and null otherwise.
   */
  stepWall(): FrameMessage | IdleMessage | null {
    const now = this.now();
    if (this.status === 'idle') {
      this.idleSeconds += 1;
      if (this.idleSeconds < IDLE_EVERY_S) return null;
      this.idleSeconds = 0;
      return { type: 'idle', status: 'idle', run_id: null, last_run: this.lastRun ? { ...this.lastRun } : null };
    }
    if (this.status === 'starting' && now - this.startedAtMs >= 1500) {
      this.status = 'running';
      for (const w of this.workers) w.status = 'healthy';
      const startS = toS(this.settings.start_utc);
      const prices =
        startS >= REAL_UNTIL_2026_S
          ? 'the AI’s predicted prices'
          : (this.settings.year ?? 2025) >= 2026
            ? 'prices (on this practice server, 2025’s real ones a year on)'
            : 'real prices';
      this.emit(
        'run_started',
        'info',
        `The replay started: ${prices} from ${spokenDate(startS)}, with ${FLEET.toLocaleString('en-US')} batteries ${Math.round(this.settings.start_charge_level * 100)}% charged.`,
      );
    }
    if (this.status === 'running') {
      for (let k = 0; k < this.settings.speed_tps; k++) {
        if (!this.segment || this.nextTickS() >= this.segment.endS) {
          this.status = 'finished';
          this.emit('run_finished', 'good', 'The replay finished: it reached the end of the loaded prices.', {
            revenue_usd: round(this.totals.revenue_usd),
          });
          break;
        }
        this.tick();
      }
    } else if (this.status === 'paused') {
      this.detectWhilePaused(now);
    }
    // A restart finishes at the next step; while nothing steps, after a second.
    if (this.status !== 'running') this.recoverPending(now);
    const forecast = this.pendingForecast;
    this.pendingForecast = null;
    const frame: FrameMessage = {
      type: 'frame',
      run_id: this.runId,
      status: this.status,
      sim_time_utc: this.simTimeUtc(),
      speed_tps: this.settings.speed_tps,
      ticks: this.pendingTicks.splice(0),
      totals: this.publicTotals(),
      workers: this.publicWorkers(),
      failed_commands: { count: this.failed.count, recent: [...this.failed.recent] },
      availability: this.availability(),
      events: this.pendingEvents.splice(0),
      battery_status: this.batteryStatus(),
      race: this.publicRace(),
    };
    if (forecast) frame.forecast = forecast;
    return frame;
  }

  snapshot(): SnapshotMessage {
    const hasRun = this.status !== 'idle';
    return {
      type: 'snapshot',
      run_id: this.runId,
      status: this.status,
      sim_time_utc: hasRun ? this.simTimeUtc() : null,
      speed_tps: this.settings.speed_tps,
      settings: hasRun ? this.settings : null,
      history: this.history.slice(-HISTORY_CAP),
      totals: this.publicTotals(),
      workers: this.publicWorkers(),
      failed_commands: { count: this.failed.count, recent: [...this.failed.recent] },
      availability: this.availability(),
      events: this.events.slice(-20),
      forecast: { ...this.forecast },
      battery_status: this.batteryStatus(),
      race: this.publicRace(),
    };
  }

  /**
   * docs/API.md Race: in every snapshot and frame while a run exists, null while idle. `potential` is a second
   * shadow fleet, the live strategy with every battery online and no crashes; `shows_potential` turns true once
   * a battery went offline or a controller crashed, and stays true for the run.
   */
  publicRace(): Race | null {
    if (this.status === 'idle' || !this.runId) return null;
    const live = this.totals.revenue_usd;
    return {
      ...this.race.toJson(live),
      potential: {
        label: `${strategyLabel(this.settings.strategy).replace(/\s*\([^)]*\)$/, '')}, all batteries online`,
        revenue_usd: round(this.potential.revenue, 4),
        by_location: perLoc((l) => ({ revenue_usd: round(this.potential.revenueOf(l), 4) })),
        shortfall_usd: round(this.potential.revenue - live, 4),
      },
      shows_potential: this.hadOutage,
    };
  }

  private publicTotals(): Totals {
    return {
      ...this.totals,
      by_location: perLoc((l) => ({
        revenue_usd: round(this.totalsBy[l].revenue_usd, 4),
        sold_kwh: round(this.totalsBy[l].sold_kwh, 3),
        bought_kwh: round(this.totalsBy[l].bought_kwh, 3),
      })),
    };
  }

  private recountWorkers(): void {
    const counts = new Uint32Array(WORKERS * LOCS.length);
    for (let g = 0; g < FLEET; g++) counts[this.owner[g] * LOCS.length + LOC_OF[g]]++;
    for (const w of this.workers) {
      let total = 0;
      const byLoc: Record<string, number> = {};
      LOCS.forEach((loc, k) => {
        const n = counts[w.worker_id * LOCS.length + k];
        if (n) byLoc[loc] = n;
        total += n;
      });
      w.batteries = total;
      w.by_location = byLoc;
    }
  }

  /**
   * docs/API.md Worker: `charge_level` is the capacity-weighted charge of the batteries a worker owns now, and
   * `usable_charge` the share above the 20% reserve; both null when it owns none (a crashed one, handed over).
   */
  private publicWorkers(): Worker[] {
    const stored = new Float64Array(WORKERS);
    const capacity = new Float64Array(WORKERS);
    for (let g = 0; g < FLEET; g++) {
      stored[this.owner[g]] += this.soc[g] * CAPACITY[g];
      capacity[this.owner[g]] += CAPACITY[g];
    }
    return this.workers.map(({ worker_id, status, pid, batteries, last_tick_utc, adopted_from, by_location }) => {
      const level = batteries > 0 && capacity[worker_id] > 0 ? stored[worker_id] / capacity[worker_id] : null;
      return {
        worker_id,
        status,
        pid,
        batteries,
        last_tick_utc,
        adopted_from: [...adopted_from],
        by_location: { ...by_location },
        charge_level: level === null ? null : round(level, 4),
        usable_charge: level === null ? null : round(usableOf(level), 4),
      };
    });
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

  // ---- one 5-minute step ------------------------------------------------------------------

  private tick(): void {
    const index = this.tickIndex;
    const tsS = this.tickS(index);
    this.currentTickUtc = isoUtc(tsS);
    const predicted = isPredicted(tsS);
    if (predicted && !this.predictionNoted) {
      // docs/API.md prediction_started: once, at the first predicted tick.
      this.predictionNoted = true;
      this.emit(
        'prediction_started',
        'info',
        'From here on, prices are the AI’s prediction, not real ERCOT data (on this practice server, a simulated one).',
        { real_until_utc: REAL_UNTIL_2026 },
      );
    }
    this.recoverPending(null);
    const dying = this.detectAtTick();

    const price = perLoc(() => 0);
    const decision = perLoc(() => 0);
    const action = perLoc<TickAction>(() => 'hold');
    const target = perLoc(() => 0);
    const actual = perLoc(() => 0);
    const revenue = perLoc(() => 0);
    const level = perLoc(() => 0);
    const started: { loc: Loc; action: TickAction; price: number }[] = [];
    const stopped: { loc: Loc; action: TickAction; offline: boolean }[] = [];
    const spikes: { loc: Loc; price: number }[] = [];

    // A controller that is not responding (or was just declared dead) sends no orders: its batteries
    // miss them (the gap), they end up as failed commands, and the map shows them as "controller down".
    const lost = new Set(this.workers.filter((w) => w.status === 'suspect').map((w) => w.worker_id));
    for (const w of dying) lost.add(w.worker_id);
    const newlyFailed: FailedCommand[] = [];

    for (const loc of LOCS) {
      price[loc] = priceAt(loc, tsS) ?? 0;
      decision[loc] = priceAt(loc, tsS - INTERVAL_S) ?? price[loc];
      let want = decide(this.settings.strategy, this.segment, loc, tsS, decision[loc]);

      // Directional headroom of each available battery, grid-side kW.
      const base = BASE[loc];
      const count = COUNT[loc];
      let headroom = 0;
      const h = new Float64Array(count);
      if (want !== 'hold') {
        for (let n = 0; n < count; n++) {
          const g = base + n;
          if (!this.isAvailable(g)) continue;
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
          let online = 0;
          for (let n = 0; n < count; n++) if (this.isAvailable(base + n)) online++;
          stopped.push({ loc, action: want, offline: online === 0 });
        }
        want = 'hold';
      }
      action[loc] = want;
      const sign = want === 'sell' ? 1 : want === 'charge' ? -1 : 0;
      target[loc] = sign * headroom;

      let delivered = 0;
      for (let n = 0; n < count; n++) {
        const g = base + n;
        const setpoint = headroom > 0 ? sign * h[n] : 0;
        if (lost.has(this.owner[g])) {
          this.lastSetpoint[g] = 0;
          if (setpoint !== 0) {
            if (newlyFailed.length < 400) {
              newlyFailed.push({
                battery_id: batteryId(g),
                worker_id: this.owner[g],
                reason: `${controller(this.owner[g])} did not respond after 3 tries`,
                tick_utc: isoUtc(tsS),
              });
            }
            this.failed.count++;
          }
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
      const by = this.totalsBy[loc];
      by.revenue_usd += revenue[loc];
      if (delivered > 0) {
        this.totals.sold_kwh += delivered * DT_H;
        by.sold_kwh += delivered * DT_H;
      } else {
        this.totals.bought_kwh += -delivered * DT_H;
        by.bought_kwh += -delivered * DT_H;
      }

      let stored = 0;
      for (let n = 0; n < count; n++) stored += this.soc[base + n] * CAPACITY[base + n];
      level[loc] = stored / CAPACITY_BY_LOC[loc];

      const prev = this.lastAction[loc];
      this.lastAction[loc] = want;
      if (prev !== want && want !== 'hold') started.push({ loc, action: want, price: decision[loc] });
      if (!this.spiked[loc] && price[loc] >= 1000 && this.segment) {
        this.spiked[loc] = true;
        spikes.push({ loc, price: price[loc] });
      }
    }
    this.noteActions(started, stopped);
    this.noteSpikes(spikes);

    if (newlyFailed.length) this.failed.recent = newlyFailed.slice(-10).toReversed().concat(this.failed.recent).slice(0, 10);

    const sum = (r: Record<Loc, number>) => LOCS.reduce((s, l) => s + r[l], 0);
    const fleetTarget = sum(target);
    const fleetActual = sum(actual);
    const fleetRevenue = sum(revenue);
    const capTotal = sum(CAPACITY_BY_LOC);
    this.totals.revenue_usd += fleetRevenue;
    this.totals.ticks += 1;

    // The race: the same step for the shadow fleet, then a race_update as a spike starts and after it ends.
    // The charge levels "as the spike began" are the last step's, before this one sells into it.
    const liveLevelBefore = this.history[this.history.length - 1]?.charge_level.fleet ?? this.settings.start_charge_level;
    const raceLevelBefore = this.race.fleetLevel;
    this.race.step(this.segment, tsS, price, decision, (g) => this.isAvailable(g));
    // What the live strategy would earn with every battery online and no crashes (race.potential).
    this.potential.step(this.segment, tsS, price, decision, () => true);
    const spikeLocs = LOCS.filter((l) => price[l] >= SPIKE_USD_MWH);
    if (spikeLocs.length && !this.inSpike) {
      this.inSpike = true;
      this.noteRaceSpike(spikeLocs, price, liveLevelBefore, raceLevelBefore);
    } else if (!spikeLocs.length && this.inSpike) {
      this.inSpike = false;
      this.noteRaceLead();
    }

    const t: Tick = {
      ts_utc: isoUtc(tsS),
      ts_local: isoLocal(tsS),
      price: { ...price },
      decision_price: { ...decision },
      action: { ...action },
      target_kw: { ...perLoc((l) => round(target[l], 1)), fleet: round(fleetTarget, 1) },
      actual_kw: { ...perLoc((l) => round(actual[l], 1)), fleet: round(fleetActual, 1) },
      error_pct: round((Math.abs(fleetActual - fleetTarget) / Math.max(Math.abs(fleetTarget), 0.005 * NAMEPLATE_KW)) * 100, 3),
      revenue_usd: { ...perLoc((l) => round(revenue[l], 4)), fleet: round(fleetRevenue, 4) },
      charge_level: {
        ...perLoc((l) => round(level[l], 4)),
        fleet: round(LOCS.reduce((s, l) => s + level[l] * CAPACITY_BY_LOC[l], 0) / capTotal, 4),
      },
      healthy_workers: this.workers.filter((w) => w.status === 'healthy').length,
      predicted,
    };
    this.history.push(t);
    if (this.history.length > HISTORY_CAP) this.history.splice(0, this.history.length - HISTORY_CAP);
    this.pendingTicks.push(t);
    for (const w of this.workers) if (w.status !== 'dead' && w.killedAtTick === null) w.last_tick_utc = t.ts_utc;
    if (index % FORECAST_EVERY_TICKS === 0) this.issueForecast(tsS);

    // This tick's codes for battery_status; then the hand-over of controllers declared dead at it.
    const codes = new Uint8Array(FLEET);
    for (let g = 0; g < FLEET; g++) {
      if (!this.isAvailable(g)) codes[g] = OFFLINE;
      else if (lost.has(this.owner[g])) codes[g] = DOWN;
      else if (this.lastSetpoint[g] > 1e-9) codes[g] = SELLING;
      else if (this.lastSetpoint[g] < -1e-9) codes[g] = CHARGING;
      else codes[g] = WAITING;
    }
    this.tickCodes = codes;
    for (const w of dying) this.handOver(w);

    this.tickIndex += 1;
    this.currentTickUtc = null;
  }

  /** One event per new action, naming every city that switched at this step. */
  private noteActions(
    started: { loc: Loc; action: TickAction; price: number }[],
    stopped: { loc: Loc; action: TickAction; offline: boolean }[],
  ): void {
    for (const act of ['sell', 'charge'] as const) {
      const group = started.filter((s) => s.action === act);
      if (!group.length) continue;
      const locs = group.map((s) => s.loc);
      const avg = group.reduce((s, x) => s + x.price, 0) / group.length;
      const where = group.length === 1 ? `${CITY_NAMES[locs[0]]}-area batteries` : `Batteries in ${cityList(locs)}`;
      const detail = { locations: locs, action: act, decision_price: Object.fromEntries(group.map((s) => [s.loc, s.price])) };
      if (act === 'sell') {
        this.emit('action_changed', 'good', `${where} started selling at ${group.length > 1 ? 'about ' : ''}${money(avg)}/MWh (${perKwh(avg)}).`, detail);
      } else {
        this.emit('action_changed', 'info', `${where} started charging: power is cheap at ${group.length > 1 ? 'about ' : ''}${money(avg)}/MWh (${perKwh(avg)}).`, detail);
      }
    }
    for (const act of ['sell', 'charge'] as const) {
      for (const offline of [false, true]) {
        const group = stopped.filter((s) => s.action === act && s.offline === offline);
        if (!group.length) continue;
        const locs = group.map((s) => s.loc);
        const where = group.length === 1 ? `${CITY_NAMES[locs[0]]}-area batteries` : `Batteries in ${cityList(locs)}`;
        const why = offline ? 'they are all offline' : act === 'sell' ? 'they are down to their 20% safety reserve' : 'they are full';
        this.emit('action_changed', 'info', `${where} stopped ${act === 'sell' ? 'selling' : 'charging'}: ${why}.`, {
          locations: locs,
          action: 'hold',
        });
      }
    }
  }

  private noteSpikes(spikes: { loc: Loc; price: number }[]): void {
    if (!spikes.length || !this.segment) return;
    const seg = this.segment;
    const top = Math.max(...spikes.map((s) => s.price));
    const times = Math.round(spikes.reduce((s, x) => s + x.price / Math.max(1, seg.cheap[x.loc]), 0) / spikes.length);
    const where = spikes.length === 1 ? `${AREA_NAMES[spikes[0].loc]} prices` : `Prices in ${cityList(spikes.map((s) => s.loc))}`;
    this.emit(
      'price_spike',
      'info',
      `${where} spiked to ${spikes.length > 1 ? 'up to ' : ''}${money(top)}/MWh (${perKwh(top)}), about ${times} times the overnight price.`,
      { locations: spikes.map((s) => s.loc), price: Object.fromEntries(spikes.map((s) => [s.loc, s.price])) },
    );
  }

  /** "Prices jumped to $1,757 in all four cities. The AI fleet had 54% charge left; the simple rule had 21%." */
  private noteRaceSpike(locs: Loc[], price: Record<Loc, number>, liveLevel: number, raceLevel: number): void {
    const top = Math.max(...locs.map((l) => price[l]));
    const where = locs.length === 1 ? CITY_NAMES[locs[0]] : cityList(locs);
    this.emit(
      'race_update',
      'info',
      `Prices jumped to ${money(top)} in ${where}. The ${spoken(this.settings.strategy)} fleet had ${Math.round(liveLevel * 100)}% charge left; the ${spoken(this.race.strategy)} had ${Math.round(raceLevel * 100)}%.`,
      {
        race_strategy: this.race.strategy,
        live_charge_level: round(liveLevel, 4),
        race_charge_level: round(raceLevel, 4),
        locations: locs,
        price: Object.fromEntries(locs.map((l) => [l, price[l]])),
      },
    );
  }

  /** "The AI is now $12,400 ahead of the simple rule." The leader comes first. */
  private noteRaceLead(): void {
    const diff = this.totals.revenue_usd - this.race.revenue;
    const abs = Math.abs(diff);
    const amount = money(abs >= 1000 ? Math.round(abs / 100) * 100 : Math.round(abs));
    const live = spoken(this.settings.strategy);
    const other = spoken(this.race.strategy);
    const [leader, behind] = diff >= 0 ? [live, other] : [other, live];
    this.emit('race_update', diff >= 0 ? 'good' : 'info', `The ${leader} is now ${amount} ahead of the ${behind}.`, {
      race_strategy: this.race.strategy,
      live_minus_race_usd: round(diff, 2),
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
          p10: round(p50 * (1 - spread)),
          p50: round(p50),
          p90: round(p50 * (1 + spread * 1.4)),
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
    if (this.forecastLog.length > 400) this.forecastLog.splice(0, this.forecastLog.length - 400);
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

  /**
   * A crashed controller misses this step's deadline: first "not responding", then, at the second
   * miss, crashed. Its batteries are down for both steps and are handed over after the second one
   * (the backend reassigns them for the next tick). Returns the controllers declared dead now.
   */
  private detectAtTick(): WorkerState[] {
    const dying: WorkerState[] = [];
    for (const w of this.workers) {
      if (w.killedAtTick === null || w.status === 'dead') continue;
      w.missed += 1;
      if (w.missed === 1) {
        w.status = 'suspect';
        const owned = this.ownedBy(w.worker_id);
        this.emit(
          'worker_suspect',
          'warn',
          `${controller(w.worker_id)} stopped responding. Its batteries${this.inCities(owned)} get no new orders while the others cover for it.`,
          { ...workerDetail(w), by_location: byLocation(owned) },
        );
      } else if (w.missed >= 2) {
        this.declareDead(w);
        dying.push(w);
      }
    }
    return dying;
  }

  private detectWhilePaused(nowMs: number): void {
    for (const w of this.workers) {
      if (w.killedAtMs === null || w.status === 'dead') continue;
      const age = nowMs - w.killedAtMs;
      if (w.status !== 'suspect' && age >= 2000) {
        w.status = 'suspect';
        w.missed = 1;
        const owned = this.ownedBy(w.worker_id);
        this.emit('worker_suspect', 'warn', `${controller(w.worker_id)} stopped sending its heartbeat.`, {
          ...workerDetail(w),
          by_location: byLocation(owned),
        });
      }
      if (age >= 6000) {
        this.declareDead(w);
        this.handOver(w);
      }
    }
  }

  private ownedBy(workerId: number): number[] {
    const out: number[] = [];
    for (let g = 0; g < FLEET; g++) if (this.owner[g] === workerId) out.push(g);
    return out;
  }

  /** " in Houston", " in San Antonio and Austin" for these batteries. */
  private inCities(idx: number[]): string {
    const locs = Object.keys(byLocation(idx)) as Loc[];
    return locs.length ? ` in ${cityList(locs)}` : '';
  }

  private declareDead(dead: WorkerState): void {
    dead.status = 'dead';
    this.emit('worker_dead', 'warn', `${controller(dead.worker_id)} crashed: it missed two 5-minute steps in a row.`, {
      ...workerDetail(dead),
      missed_ticks: 2,
    });
  }

  /** Round-robin in id order over the healthy controllers, as coordinator._reassign does. */
  private handOver(dead: WorkerState): void {
    const survivors = this.workers.filter((w) => w.status === 'healthy');
    const moved = this.ownedBy(dead.worker_id);
    if (!survivors.length) return;
    moved.forEach((g, i) => {
      const to = survivors[i % survivors.length];
      this.owner[g] = to.worker_id;
      if (this.offlineWorkers.has(dead.worker_id)) this.offWorker[g] = 1;
    });
    for (const s of survivors) {
      if (!s.adopted_from.includes(dead.worker_id)) s.adopted_from.push(dead.worker_id);
    }
    this.recountWorkers();
    const ms = 1 + Math.round(this.random() * 3);
    this.emit(
      'worker_adopted',
      'good',
      `${controller(dead.worker_id)}'s ${moved.length.toLocaleString('en-US')} batteries${this.inCities(moved)} were handed to the other ${survivors.length} controllers in ${ms} ms.`,
      {
        ...workerDetail(dead),
        batteries: moved.length,
        adopted_by: survivors.map((s) => s.worker_id),
        ms,
        by_location: byLocation(moved),
      },
    );
  }

  // ---- full-year results (SIMULATED placeholders) -----------------------------------------

  private isRunning(job: Job): boolean {
    return !job.cancelled && this.now() < job.doneAtMs;
  }

  /** POST /backtest {"year"}: a full-year re-run of that year (the pitch year without one). */
  startBacktest(year: number = PITCH_YEAR): { job_id: string } {
    if (!RESULT_YEARS.includes(year)) throw new MockError(400, 'invalid_year', 'The year must be 2025 or 2026.');
    // While a job runs, POST /backtest returns that job's id (docs/API.md).
    if (this.runningJob) {
      const job = this.jobs.get(this.runningJob);
      if (job && this.isRunning(job)) return { job_id: job.id };
    }
    const id = crypto.randomUUID();
    this.jobs.set(id, { id, doneAtMs: this.now() + this.backtestMs, fail: this.backtestFails, cancelled: false, year });
    this.runningJob = id;
    return { job_id: id };
  }

  /**
   * POST /backtest/{job_id}/cancel: stops a running job; nothing is stored and the previous results stay.
   * 409 not_running once it has finished (or was stopped already), 404 for an unknown id.
   */
  cancelBacktest(jobId: string): { status: 'cancelled' } {
    const job = this.jobs.get(jobId);
    if (!job) throw new MockError(404, 'unknown_job', 'No full-year run has that ID.');
    if (!this.isRunning(job)) throw new MockError(409, 'not_running', 'That full-year run is not running any more.');
    job.cancelled = true;
    if (this.runningJob === job.id) this.runningJob = null;
    this.emit('backtest_done', 'info', 'The full-year re-run was stopped. The previous results are unchanged.', {
      job_id: job.id,
      status: 'cancelled',
    });
    return { status: 'cancelled' };
  }

  backtestStatus(jobId: string): BacktestStatus {
    const job = this.jobs.get(jobId);
    if (!job) throw new MockError(404, 'unknown_job', 'No full-year run has that ID.');
    if (job.cancelled) return { status: 'cancelled', run_ids: [], skipped: {}, error: null };
    if (this.now() < job.doneAtMs) return { status: 'running', run_ids: [], skipped: {}, error: null };
    if (job.fail) {
      return { status: 'failed', run_ids: [], skipped: {}, error: 'The practice server was told to fail this run (MOCK_BACKTEST_FAIL=1).' };
    }
    this.missing.delete(job.year);
    return { status: 'done', run_ids: ['mock-bands', 'mock-forecast', 'mock-peak'], skipped: {}, error: null };
  }

  /** 404 no_results for a year without results (or one this server does not have). */
  private resultsYear(year: number | null): number {
    const y = year ?? PITCH_YEAR;
    if (!RESULT_YEARS.includes(y) || this.missing.has(y)) throw new MockError(404, 'no_results', 'No full-year results yet.');
    return y;
  }

  /**
   * GET /results/pitch?year=: 2025's per-battery figures are the measured ones (RUNLOG.md, 4-city full-year
   * results; ./gains.ts); 2026's are SIMULATED, split at real_until_utc into their real and projected parts
   * (docs/API.md). Cycles, top days and most city splits are made up for both.
   */
  results(year: number | null = null): Results {
    const y = this.resultsYear(year);
    const powerMw = NAMEPLATE_KW / 1000;
    const energyMwh = CAPACITY.reduce((a, b) => a + b, 0) / 1000;
    const avgKw = NAMEPLATE_KW / FLEET;
    const avgKwh = (energyMwh * 1000) / FLEET;
    const factor: Record<Loc, number> = { LZ_HOUSTON: 0.94, LZ_NORTH: 0.98, LZ_CPS: 1.11, LZ_AEN: 1.12 };
    const measured = MEASURED.find((m) => m.year === y) ?? MEASURED[0];
    const split = y === 2026;
    const perBattery = (key: 'bands' | 'forecast' | 'peak') => measured.strategies[key].perBattery;
    // docs/API.md: real + projected = per_battery_usd_year; a fully real year has projected 0.
    const parts = (key: 'bands' | 'forecast' | 'peak' | 'best', whole: number) =>
      split
        ? { real_per_battery_usd: SPLIT_2026[key].real, projected_per_battery_usd: SPLIT_2026[key].projected }
        : { real_per_battery_usd: whole, projected_per_battery_usd: 0 };
    const dayScale = split ? 1.06 : 1;
    const strategy = (
      key: 'bands' | 'forecast' | 'peak',
      label: string,
      cycles: number,
      headline: boolean,
      days: number[],
      perCity?: Record<Loc, number>,
    ) => ({
      strategy: key,
      label,
      run_id: `mock-${key}-${y}`,
      is_headline: headline,
      per_battery_usd_year: perBattery(key),
      ...parts(key, perBattery(key)),
      fleet_usd_year: Math.round(perBattery(key) * FLEET),
      usd_per_kwh_year: round(perBattery(key) / avgKwh),
      usd_per_kw_year: round(perBattery(key) / avgKw),
      cycles_per_year: cycles,
      per_location_usd: perLoc((l) => Math.round((perCity && !split ? perCity[l] : perBattery(key) * factor[l]) * COUNT[l])),
      top_days: (split ? TOP_DAY_DATES_2026 : TOP_DAY_DATES).map((date_local, i) => ({ date_local, usd: Math.round(days[i] * dayScale) })),
    });
    return {
      year: y,
      generated_utc: new Date(this.now()).toISOString(),
      fleet_size: FLEET,
      fleet_power_mw: round(powerMw, 1),
      fleet_energy_mwh: round(energyMwh, 1),
      real_until_utc: split ? REAL_UNTIL_2026 : null,
      assumptions: [
        split
          ? 'SIMULATED practice data from the mock server: every 2026 figure is made up (no 2026 backtest exists here), split at Sep 20 into a real part and a part projected on the AI’s predicted prices.'
          : 'SIMULATED practice data from the mock server: the per-battery figures and the AI forecast’s city figures are copied from the real 2025 backtest (RUNLOG.md); the cycles, top days and other city splits are made up.',
        'Gross wholesale energy value at ERCOT real-time prices (15-minute settlement).',
      ],
      strategies: [
        strategy('bands', 'Simple rule', 286, false, [182_000, 121_500, 96_200, 88_900, 71_300, 64_800, 60_100, 55_700, 49_900, 47_200]),
        // RUNLOG.md: the AI forecast per battery in 2025, by city.
        strategy('forecast', 'AI forecast (TimesFM-3)', 301, true, [412_600, 318_400, 205_700, 164_900, 121_800, 98_300, 90_600, 84_100, 77_500, 70_900], {
          LZ_HOUSTON: 179.49,
          LZ_NORTH: 187.17,
          LZ_CPS: 212.77,
          LZ_AEN: 213.32,
        }),
        strategy('peak', 'Evening peak', 365, false, [301_200, 226_000, 150_400, 118_800, 96_500, 80_200, 72_100, 66_400, 61_000, 58_300]),
      ],
      ceiling: {
        label: 'Best possible (perfect hindsight)',
        per_battery_usd_year: measured.bestPerBattery,
        ...parts('best', measured.bestPerBattery),
        fleet_usd_year: Math.round(measured.bestPerBattery * FLEET),
      },
    };
  }

  /** GET /results/ai-gains?year=: that year's numbers (./gains.ts); 404 no_results like GET /results/pitch. */
  aiGains(year: number | null = null): AiGains {
    const y = this.resultsYear(year);
    if (this.noAiGains) throw new MockError(404, 'no_results', 'No full-year results yet.');
    return mockAiGains(FLEET, year === null ? null : y);
  }
}

/** Simulated top days (dates chosen from known 2025 spike days; the dollar values are made up). */
const TOP_DAY_DATES = [
  '2025-04-07',
  '2025-05-16',
  '2025-07-11',
  '2025-01-15',
  '2025-08-19',
  '2025-06-24',
  '2025-02-20',
  '2025-07-29',
  '2025-09-03',
  '2025-05-13',
];

/** 2026's (all made up): some fall after Sep 20, on the AI's predicted prices. */
const TOP_DAY_DATES_2026 = [
  '2026-08-18',
  '2026-05-12',
  '2026-10-06',
  '2026-07-24',
  '2026-01-21',
  '2026-11-17',
  '2026-06-30',
  '2026-09-02',
  '2026-12-09',
  '2026-04-15',
];

/** GET /settings with the pinned item shapes (docs/API.md). */
export function mockSettingsResponse() {
  return {
    defaults: DEFAULT_SETTINGS,
    editable: ['year', 'preset', 'speed_tps', 'strategy', 'start_charge_level'],
    presets: PRESETS,
    years: YEARS,
    strategies: STRATEGIES,
    speeds: SPEEDS.map((tps) => ({ tps, label: `1 hour every ${12 / tps} s` })),
  };
}
