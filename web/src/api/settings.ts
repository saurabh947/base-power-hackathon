// Reading GET /settings (docs/API.md): Preset {id, label, date_local, start_utc,
// start_local, description}, Strategy {id, label, description}, Speed {tps, label}.
// Items that do not match are skipped, and a missing or broken response falls
// back to the documented defaults, so the Start screen always works.

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
  preset: 'ai_best_evening',
  start_utc: '2025-05-30T23:00:00Z',
  speed_tps: 2,
  strategy: 'forecast',
  start_charge_level: 0.9,
  fleet_size: 20000,
  power_kw_range: [5.0, 11.5],
  capacity_kwh_range: [10.0, 20.0],
  locations: { LZ_HOUSTON: 0.35, LZ_NORTH: 0.4, LZ_CPS: 0.13, LZ_AEN: 0.12 },
};

const DOCUMENTED_PRESETS: Preset[] = [
  {
    id: 'ai_best_evening',
    label: "The AI's best evening",
    date_local: '2025-05-30',
    start_utc: '2025-05-30T23:00:00Z',
    start_local: '2025-05-30T18:00:00-05:00',
    description: 'The 2025 evening where the AI forecast pulled furthest ahead of the simple rule.',
  },
  {
    id: 'summer_spike',
    label: 'Summer night spike',
    date_local: '2025-07-11',
    start_utc: '2025-07-11T23:00:00Z',
    start_local: '2025-07-11T18:00:00-05:00',
    description: 'Both areas jump to about $1,750/MWh at 9 pm.',
  },
  {
    id: 'spike_evening',
    label: 'Price-spike evening',
    date_local: '2025-05-16',
    start_utc: '2025-05-16T21:00:00Z',
    start_local: '2025-05-16T16:00:00-05:00',
    description: 'Prices jump above $2,000/MWh around 7:45 pm.',
  },
];

const DEFAULT_SPEEDS = [1, 2, 5, 10];

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Speed {tps, label}. The label shown is always our own plain phrase for the same tps. */
export function readSpeeds(raw: unknown): SpeedOption[] {
  const values = Array.isArray(raw)
    ? raw.map((s) => (isObject(s) ? Number(s.tps) : Number.NaN)).filter((n) => Number.isFinite(n) && n > 0)
    : [];
  const list = values.length ? [...new Set(values)] : DEFAULT_SPEEDS;
  return list.toSorted((a, b) => a - b).map((value) => ({ value, label: fmtSpeed(value) }));
}

/** Strategy {id, label, description}; the glossary fills in a missing label or description. */
export function readStrategies(raw: unknown): StrategyOption[] {
  const out: StrategyOption[] = [];
  for (const s of Array.isArray(raw) ? raw : []) {
    if (!isObject(s) || typeof s.id !== 'string' || out.some((o) => o.value === s.id)) continue;
    out.push({
      value: s.id,
      label: typeof s.label === 'string' && s.label ? s.label : strategyName(s.id),
      description: typeof s.description === 'string' ? s.description : (STRATEGY_DESCRIPTIONS[s.id] ?? ''),
    });
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
  const out = raw.filter(
    (p): p is Preset =>
      isObject(p) && typeof p.id === 'string' && typeof p.label === 'string' && typeof p.start_utc === 'string',
  );
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
