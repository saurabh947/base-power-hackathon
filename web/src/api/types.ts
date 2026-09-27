// Types for the fleet-sim API contract, v1 (docs/API.md). Field names match the
// wire format exactly. Units: power kW, energy kWh, prices $/MWh, money USD.
// Signs: power is + when selling to the grid and - when charging.

/**
 * The Texas Triangle load zones, in config order (the battery id blocks run in this order).
 * The live list comes from GET /fleet/layout (src/lib/cities.ts); this is the documented default.
 */
export type LocationId = 'LZ_HOUSTON' | 'LZ_NORTH' | 'LZ_CPS' | 'LZ_AEN';
export const LOCATIONS: readonly LocationId[] = ['LZ_HOUSTON', 'LZ_NORTH', 'LZ_CPS', 'LZ_AEN'];

export type RunStatus = 'idle' | 'starting' | 'running' | 'paused' | 'finished';
export type StrategyId = 'bands' | 'forecast' | 'peak';
export type TickAction = 'charge' | 'sell' | 'hold';
export type WorkerStatus = 'starting' | 'healthy' | 'suspect' | 'dead';
export type Severity = 'info' | 'good' | 'warn';

/** Keyed by location id; `fleet` is the whole-fleet total where the API sends one. */
export type ByLocation<T> = Partial<Record<string, T>>;

export interface Settings {
  /** 2025 or 2026 (docs/API.md). Missing from an older backend: the preset's year. */
  year?: number;
  preset: string;
  start_utc: string;
  speed_tps: number;
  strategy: string;
  start_charge_level: number;
  fleet_size: number;
  power_kw_range: [number, number];
  capacity_kwh_range: [number, number];
  locations: Record<string, number>;
}

/** The only keys visitors may change; the server ignores the rest. */
export type EditableKey = 'year' | 'preset' | 'speed_tps' | 'strategy' | 'start_charge_level';
export const EDITABLE_KEYS: readonly EditableKey[] = ['year', 'preset', 'speed_tps', 'strategy', 'start_charge_level'];
/** What the Start screen's editor holds: always a year (the preset's). */
export interface EditableSettings {
  year: number;
  preset: string;
  speed_tps: number;
  strategy: string;
  start_charge_level: number;
}

export interface Preset {
  id: string;
  /** The year the preset belongs to (docs/API.md); an older backend leaves it out. */
  year?: number;
  label: string;
  date_local: string;
  start_utc: string;
  start_local?: string;
  description: string;
}

/**
 * GET /settings `years[]`: 2025 is real prices all year; 2026 is real prices until `real_until_utc` (the end of
 * the last real interval), then the AI's prediction.
 */
export interface YearInfo {
  year: number;
  label: string;
  description: string;
  real_until_utc: string | null;
}

export interface StrategyChoice {
  id: string;
  label: string;
  description: string;
}

export interface SpeedChoice {
  tps: number;
  label: string;
}

export interface SettingsResponse {
  defaults: Settings;
  editable: string[];
  presets: Preset[];
  strategies: StrategyChoice[];
  speeds: SpeedChoice[];
  years?: YearInfo[];
}

export interface Tick {
  ts_utc: string;
  ts_local: string;
  price: ByLocation<number>;
  decision_price?: ByLocation<number>;
  action: ByLocation<TickAction>;
  target_kw: ByLocation<number>;
  actual_kw: ByLocation<number>;
  error_pct: number;
  revenue_usd: ByLocation<number>;
  charge_level: ByLocation<number>;
  healthy_workers: number;
  /**
   * True after the year's real_until_utc (2026): the prices are the AI's prediction (TimesFM-3, p50), not
   * ERCOT's, and the money earned on this tick is a projection. Missing means real.
   */
  predicted?: boolean;
}

export interface LocationTotals {
  revenue_usd: number;
  sold_kwh: number;
  bought_kwh: number;
}

export interface Totals extends LocationTotals {
  ticks: number;
  /** The same sums per location. */
  by_location?: Record<string, LocationTotals>;
}

export interface Worker {
  worker_id: number;
  status: WorkerStatus;
  pid: number | null;
  batteries: number;
  last_tick_utc: string | null;
  adopted_from: number[];
  /** How many of its batteries are in each location, e.g. {"LZ_NORTH": 400, …}. */
  by_location?: Record<string, number>;
  /** Capacity-weighted average charge of the batteries it owns now, 0.2-1.0; null when it owns none. */
  charge_level?: number | null;
  /**
   * Usable energy left above the 20% reserve, 0-1: (charge_level − 0.2) / 0.8; null when it owns none. The
   * controller panel shows this. A crash empties a controller's batteries (0); taking its batteries offline
   * halves it (docs/API.md, failure model). Missing from an older backend.
   */
  usable_charge?: number | null;
}

/** POST /workers/{worker_id}/restart: a crashed controller is coming back. */
export interface RestartResponse {
  worker_id: number;
  status: WorkerStatus;
}

export interface FailedCommand {
  battery_id: string;
  worker_id: number;
  reason: string;
  tick_utc: string;
}

export interface FailedCommands {
  count: number;
  recent: FailedCommand[];
}

export interface AreaAvailability {
  available: number;
  total: number;
}

export interface Availability {
  available: number;
  total: number;
  by_location: Record<string, AreaAvailability>;
  offline_workers: number[];
}

export interface LiveEvent {
  id: string;
  wall_utc: string;
  sim_utc: string | null;
  kind: string;
  severity: Severity;
  message: string;
  /**
   * Technical values. Worker events carry `worker_id` (0-based) and `controller` (1-based);
   * `worker_adopted` adds `adopted_by` (worker ids) and `ms`.
   */
  detail?: Record<string, unknown>;
}

export interface ForecastPoint {
  ts_utc: string;
  p10: number;
  p50: number;
  p90: number;
}

export interface Forecast {
  location: string;
  series: string;
  model: string;
  issued_utc: string;
  step_minutes: number;
  points: ForecastPoint[];
}

/** One city of the race fleet. */
export interface RaceLocation {
  revenue_usd: number;
  charge_level: number;
  action: TickAction;
}

/**
 * The race (docs/API.md): a shadow copy of the fleet running the comparison strategy side by side with
 * the live one, on the same batteries, prices, decision lag and availability (not the controller crashes).
 * The comparison is `bands` ("Simple rule"), or `forecast` when the live run itself uses `bands`.
 */
export interface Race {
  strategy: string;
  label: string;
  /** The race fleet's earnings so far (a fleet total). */
  revenue_usd: number;
  by_location: Record<string, RaceLocation>;
  /** 0-1, the race fleet's charge level now. */
  charge_level: number;
  /** The live fleet's totals.revenue_usd minus the race's: positive means the live strategy is ahead. */
  live_minus_race_usd: number;
  /** A second shadow fleet: the live strategy with every battery online and no controller crashes. */
  potential?: RacePotential | null;
  /** True (for the rest of the run) once any battery went offline or any controller crashed: show `potential`. */
  shows_potential?: boolean;
}

export interface RacePotential {
  /** "AI forecast, all batteries online". */
  label: string;
  revenue_usd: number;
  by_location?: Record<string, { revenue_usd: number }>;
  /** potential.revenue_usd minus the live revenue: what outages cost so far. */
  shortfall_usd: number;
}

interface LiveBody {
  run_id: string | null;
  status: RunStatus;
  totals: Totals;
  workers: Worker[];
  failed_commands: FailedCommands;
  availability: Availability;
  events: LiveEvent[];
  forecast?: Record<string, Forecast | null | undefined>;
  /** Base64 of packed 4-bit codes, one per battery (src/map/status.ts). */
  battery_status?: string;
  /** Null while idle. */
  race?: Race | null;
}

export interface SnapshotMessage extends LiveBody {
  type: 'snapshot';
  settings: Settings | null;
  history: Tick[];
  /** Null while idle. */
  sim_time_utc: string | null;
  speed_tps: number;
}

export interface FrameMessage extends LiveBody {
  type: 'frame';
  sim_time_utc: string;
  speed_tps: number;
  ticks: Tick[];
}

/** Sent every 5 s while no run exists. `last_run` sums up the run that was replaced or reset, if any. */
export interface IdleMessage {
  type: 'idle';
  status: 'idle';
  run_id: null;
  last_run: { run_id: string; totals: Totals } | null;
}

export type LiveMessage = SnapshotMessage | FrameMessage | IdleMessage;

export interface HealthResponse {
  ok: boolean;
  version: string;
  status: RunStatus;
}

export interface StartResponse {
  run_id: string;
  settings: Settings;
}

export type AvailabilityRequest =
  | { scope: 'location'; location: string; offline_fraction: number }
  | { scope: 'worker'; worker_id: number; available: boolean }
  | { scope: 'battery'; battery_id: string; available: boolean }
  | { scope: 'all'; available: true };

export interface AvailabilityResponse {
  applied_from_tick_utc: string;
  affected: number;
  available: number;
  total: number;
}

export interface BatteryInfo {
  battery_id: string;
  location: string;
  /** City name shown to people, e.g. "Houston". */
  city?: string;
  /** Simulated position, [longitude, latitude]. */
  lonlat?: [number, number];
  worker_id: number;
  capacity_kwh: number;
  power_kw: number;
  charge_level: number;
  power_kw_now: number;
  available: boolean;
}

/** GET /fleet/layout: where every battery sits, for the map. Static per fleet; cache it by `version`. */
export interface LayoutCity {
  location: string;
  name: string;
  short: string;
  batteries: number;
  first_battery: number;
  center_lonlat: [number, number];
}

export interface FleetLayout {
  version: string;
  fleet_size: number;
  cities: LayoutCity[];
  /** Base64 of a little-endian Int16Array [lon0, lat0, lon1, lat1, …]: lon = -100 + v/1000, lat = 31 + v/1000. */
  positions: string;
}

export interface KillResponse {
  worker_id: number;
  pid: number;
  killed_at_utc: string;
}

export interface TopDay {
  date_local: string;
  usd: number;
}

/**
 * A year with an AI-predicted part (2026, docs/API.md Results): the per-battery figure split at real_until_utc
 * into what real prices earned and what the AI's predicted prices project. The two add up to
 * per_battery_usd_year; for a fully real year projected is 0. Missing from an older backend.
 */
interface RealProjectedSplit {
  real_per_battery_usd?: number;
  projected_per_battery_usd?: number;
}

export interface StrategyResult extends RealProjectedSplit {
  strategy: string;
  label: string;
  run_id: string;
  is_headline: boolean;
  per_battery_usd_year: number;
  fleet_usd_year: number;
  usd_per_kwh_year: number;
  usd_per_kw_year: number;
  cycles_per_year: number;
  per_location_usd: Record<string, number>;
  top_days: TopDay[];
}

export interface Ceiling extends RealProjectedSplit {
  label: string;
  per_battery_usd_year: number;
  fleet_usd_year: number;
}

export interface Results {
  year: number;
  generated_utc: string;
  fleet_size: number;
  fleet_power_mw: number;
  fleet_energy_mwh: number;
  assumptions: string[];
  strategies: StrategyResult[];
  ceiling: Ceiling | null;
  /** The end of the last real interval (2026); null or missing when the whole year is real prices. */
  real_until_utc?: string | null;
}

/** One decision method's full year in GET /results/ai-gains. Energy in MWh, prices in $/MWh. */
export interface StrategyGains {
  label: string;
  per_battery_usd_year: number;
  fleet_usd_year: number;
  /** Per-battery earnings ÷ the same year's hindsight best, 0-1. */
  share_of_best: number;
  /** Energy bought at prices ≤ $0/MWh. */
  surplus_mwh: number;
  /** Energy sold during 5-minute prices ≥ $1,000/MWh. */
  spike_mwh: number;
  /** How many separate spikes the year had. */
  spike_events: number;
  /** The fleet's charge level when each spike began, averaged, 0-1. */
  spike_charge_level: number;
  sold_mwh: number;
  bought_mwh: number;
  avg_sell_usd_mwh: number;
  avg_buy_usd_mwh: number;
}

/** What the AI adds over the baseline in one year. `pct` is a fraction: 0.544 means +54.4%. */
export interface Uplift {
  per_battery_usd_year: number;
  pct: number;
  fleet_usd_year: number;
}

export interface AiGainsYear {
  year: number;
  is_pitch: boolean;
  best_possible_per_battery_usd_year: number;
  /** Keyed by strategy id: forecast, bands, peak. */
  strategies: Record<string, StrategyGains>;
  uplift: Uplift;
}

/** GET /results/ai-gains: what the AI (`ai`) adds over the simple rule (`baseline`), the pitch year first. */
export interface AiGains {
  ai: string;
  baseline: string;
  years: AiGainsYear[];
}

export interface ApiErrorBody {
  error?: string;
  message?: string;
  retry_after_s?: number;
}
