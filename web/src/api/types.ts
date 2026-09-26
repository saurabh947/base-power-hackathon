// Types for the fleet-sim API contract, v1 (docs/API.md). Field names match the
// wire format exactly. Units: power kW, energy kWh, prices $/MWh, money USD.
// Signs: power is + when selling to the grid and - when charging.

export type LocationId = 'LZ_HOUSTON' | 'LZ_NORTH';
export const LOCATIONS: readonly LocationId[] = ['LZ_HOUSTON', 'LZ_NORTH'];

export type RunStatus = 'idle' | 'starting' | 'running' | 'paused' | 'finished';
export type StrategyId = 'bands' | 'forecast' | 'peak';
export type TickAction = 'charge' | 'sell' | 'hold';
export type WorkerStatus = 'starting' | 'healthy' | 'suspect' | 'dead';
export type Severity = 'info' | 'good' | 'warn';

/** Keyed by location id; `fleet` is the whole-fleet total where the API sends one. */
export type ByLocation<T> = Partial<Record<string, T>>;

export interface Settings {
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
export type EditableKey = 'preset' | 'speed_tps' | 'strategy' | 'start_charge_level';
export const EDITABLE_KEYS: readonly EditableKey[] = ['preset', 'speed_tps', 'strategy', 'start_charge_level'];
export type EditableSettings = Pick<Settings, EditableKey>;

export interface Preset {
  id: string;
  label: string;
  date_local: string;
  start_utc: string;
  start_local?: string;
  description: string;
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
}

export interface Totals {
  revenue_usd: number;
  sold_kwh: number;
  bought_kwh: number;
  ticks: number;
}

export interface Worker {
  worker_id: number;
  status: WorkerStatus;
  pid: number | null;
  batteries: number;
  last_tick_utc: string | null;
  adopted_from: number[];
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

interface LiveBody {
  run_id: string | null;
  status: RunStatus;
  totals: Totals;
  workers: Worker[];
  failed_commands: FailedCommands;
  availability: Availability;
  events: LiveEvent[];
  forecast?: Record<string, Forecast | null | undefined>;
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
  worker_id: number;
  capacity_kwh: number;
  power_kw: number;
  charge_level: number;
  power_kw_now: number;
  available: boolean;
}

export interface KillResponse {
  worker_id: number;
  pid: number;
  killed_at_utc: string;
}

export interface BacktestStatus {
  status: 'running' | 'done' | 'failed';
  run_ids: string[];
  /** Strategies left out of the run, with the reason, e.g. {"forecast": "forecasts do not cover 2025"}. */
  skipped?: Record<string, string>;
  error: string | null;
}

export interface TopDay {
  date_local: string;
  usd: number;
}

export interface StrategyResult {
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

export interface Ceiling {
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
}

export interface ApiErrorBody {
  error?: string;
  message?: string;
  retry_after_s?: number;
}
