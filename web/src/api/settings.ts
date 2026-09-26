// Reading GET /settings. docs/API.md fixes `defaults`, `editable` and `presets`
// but not the shape of a Choice, so the readers below accept the likely shapes
// (bare values, {value}, {id}) and fall back to the documented defaults when the
// endpoint is missing, so the Start screen always works.

import { fmtSpeed, strategyName } from '../lib/format.ts';
import { EDITABLE_KEYS, type EditableKey, type EditableSettings, type Preset, type Settings } from './types.ts';

export interface SpeedOption {
  value: number;
  label: string;
}

export interface StrategyOption {
  value: string;
  label: string;
  description: string;
}

export interface SettingsModel {
  defaults: Settings;
  editable: EditableKey[];
  presets: Preset[];
  strategies: StrategyOption[];
  speeds: SpeedOption[];
  /** True when GET /settings failed and the documented defaults are shown instead. */
  fallback: boolean;
}

const STRATEGY_DESCRIPTIONS: Record<string, string> = {
  bands:
    'Buys when the price is in the cheapest quarter of the past week and sells when it is in the most expensive tenth.',
  forecast: 'Uses an AI price forecast to save its charge for the most expensive hours.',
  peak: 'Charges from 10 am to 3 pm and sells from 6 pm to 9 pm, every day.',
};

/** The documented defaults (docs/API.md, config.yaml), used only if GET /settings fails. */
export const DOCUMENTED_DEFAULTS: Settings = {
  preset: 'spike_evening',
  start_utc: '2025-05-16T21:00:00Z',
  speed_tps: 2,
  strategy: 'forecast',
  start_charge_level: 0.9,
  fleet_size: 20000,
  power_kw_range: [5.0, 11.5],
  capacity_kwh_range: [10.0, 20.0],
  locations: { LZ_HOUSTON: 0.5, LZ_NORTH: 0.5 },
};

const DOCUMENTED_PRESETS: Preset[] = [
  {
    id: 'spike_evening',
    label: 'Price-spike evening',
    date_local: '2025-05-16',
    start_utc: '2025-05-16T21:00:00Z',
    description: 'Prices jump above $2,000/MWh around 7:45 pm.',
  },
];

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function pick(obj: Record<string, unknown>, keys: string[]): unknown {
  for (const k of keys) if (obj[k] !== undefined && obj[k] !== null) return obj[k];
  return undefined;
}

export function readSpeeds(raw: unknown): SpeedOption[] {
  const values: number[] = [];
  if (Array.isArray(raw)) {
    for (const item of raw) {
      const v = isObject(item) ? pick(item, ['value', 'speed_tps', 'id', 'tps']) : item;
      const n = typeof v === 'string' ? Number(v) : v;
      if (typeof n === 'number' && Number.isFinite(n) && n > 0 && !values.includes(n)) values.push(n);
    }
  }
  const list = values.length ? values : [1, 2, 5, 10];
  return list.toSorted((a, b) => a - b).map((value) => ({ value, label: fmtSpeed(value) }));
}

export function readStrategies(raw: unknown): StrategyOption[] {
  const out: StrategyOption[] = [];
  if (Array.isArray(raw)) {
    for (const item of raw) {
      const value = isObject(item) ? pick(item, ['value', 'strategy', 'id']) : item;
      if (typeof value !== 'string' || out.some((o) => o.value === value)) continue;
      const label = isObject(item) && typeof item.label === 'string' ? item.label : strategyName(value);
      const description =
        isObject(item) && typeof item.description === 'string'
          ? item.description
          : (STRATEGY_DESCRIPTIONS[value] ?? '');
      out.push({ value, label, description });
    }
  }
  if (out.length) return out;
  return ['bands', 'forecast', 'peak'].map((value) => ({
    value,
    label: strategyName(value),
    description: STRATEGY_DESCRIPTIONS[value],
  }));
}

export function readPresets(raw: unknown): Preset[] {
  if (!Array.isArray(raw)) return DOCUMENTED_PRESETS;
  const out = raw.filter((p): p is Preset => isObject(p) && typeof p.id === 'string' && typeof p.label === 'string');
  return out.length ? out : DOCUMENTED_PRESETS;
}

export function readEditable(raw: unknown): EditableKey[] {
  if (!Array.isArray(raw)) return [...EDITABLE_KEYS];
  const keys = EDITABLE_KEYS.filter((k) => raw.includes(k));
  return keys.length ? keys : [...EDITABLE_KEYS];
}

export function readSettingsResponse(raw: unknown): SettingsModel {
  const obj = isObject(raw) ? raw : {};
  const defaults = isObject(obj.defaults) ? { ...DOCUMENTED_DEFAULTS, ...(obj.defaults as Partial<Settings>) } : DOCUMENTED_DEFAULTS;
  return {
    defaults,
    editable: readEditable(obj.editable),
    presets: readPresets(obj.presets),
    strategies: readStrategies(obj.strategies),
    speeds: readSpeeds(obj.speeds),
    fallback: !isObject(obj.defaults),
  };
}

export const FALLBACK_SETTINGS: SettingsModel = { ...readSettingsResponse(null), fallback: true };

/** Only the keys visitors may change (the server ignores the rest anyway). */
export function editablePart(draft: EditableSettings, editable: EditableKey[]): Partial<EditableSettings> {
  const out: Partial<EditableSettings> = {};
  for (const key of editable) (out as Record<string, unknown>)[key] = draft[key];
  return out;
}

export function presetById(presets: Preset[], id: string): Preset | undefined {
  return presets.find((p) => p.id === id);
}
