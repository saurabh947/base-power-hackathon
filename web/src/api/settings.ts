// Reading GET /settings (docs/API.md): Preset {id, year, label, date_local, start_utc, start_local,
// description}, Year {year, label, description, real_until_utc}, Strategy {id, label, description},
// Speed {tps, label}. Items that do not match are skipped, and a missing or broken response falls back to
// the documented defaults, so the Start screen always works.
//
// Years: a preset belongs to one year, and picking a year offers that year's presets. Visitors choose
// 2025 or 2026 (the human: "no 2024"), so earlier years are never offered.

import { fmtSpeed, localDateOf, strategyName } from '../lib/format.ts';
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

export interface YearOption {
  year: number;
  /** "2026". */
  label: string;
  /** "Real prices through Sep 20, then the AI's prediction." */
  description: string;
  /** End of the last real interval; null when the whole year is real prices. */
  realUntilUtc: string | null;
}

export interface SettingsModel {
  defaults: Settings;
  editable: EditableKey[];
  presets: Preset[];
  /** The years visitors can pick, oldest first; each has at least one preset. */
  years: YearOption[];
  strategies: StrategyOption[];
  speeds: SpeedOption[];
  /** True when GET /settings failed and the documented defaults are shown instead. */
  fallback: boolean;
}

/** The first year visitors may pick. */
export const FIRST_YEAR = 2025;

const STRATEGY_DESCRIPTIONS: Record<string, string> = {
  bands:
    'Buys when the price is in the cheapest quarter of the past week and sells when it is in the most expensive tenth.',
  forecast: 'Uses an AI price forecast to save its charge for the most expensive hours.',
  peak: 'Charges from 10 am to 3 pm and sells from 6 pm to 9 pm, every day.',
};

/** The documented defaults (docs/API.md, config.yaml), used only if GET /settings fails. */
export const DOCUMENTED_DEFAULTS: Settings = {
  year: 2025,
  preset: 'morning_spike',
  start_utc: '2025-04-07T08:00:00Z',
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
    id: 'morning_spike',
    year: 2025,
    label: 'Biggest spike of 2025',
    date_local: '2025-04-07',
    start_utc: '2025-04-07T08:00:00Z',
    start_local: '2025-04-07T03:00:00-05:00',
    description: "The year's highest prices: all four cities pass $3,500/MWh just before 7 am.",
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
    description: 'The whole year from its first hour.',
  },
  {
    id: 'spike_2026',
    year: 2026,
    label: 'Biggest evening spike of 2026',
    date_local: '2026-04-24',
    start_utc: '2026-04-24T21:00:00Z',
    start_local: '2026-04-24T16:00:00-05:00',
    description: 'All four cities jump above $1,000/MWh at 7:45 pm; Dallas–Fort Worth tops $1,200.',
  },
  {
    id: 'jan1_2026',
    year: 2026,
    label: 'From January 1',
    date_local: '2026-01-01',
    start_utc: '2026-01-01T06:00:00Z',
    start_local: '2026-01-01T00:00:00-06:00',
    description: 'Real prices through Sep 20, then the AI’s prediction.',
  },
];

/** docs/API.md Year items. */
const DOCUMENTED_YEARS: YearOption[] = [
  { year: 2025, label: '2025', description: 'Real ERCOT prices for the whole year.', realUntilUtc: null },
  {
    year: 2026,
    label: '2026',
    description: 'Real prices through Sep 20, then the AI’s prediction.',
    realUntilUtc: '2026-09-21T05:00:00Z',
  },
];

const DEFAULT_SPEEDS = [1, 2, 5, 10];

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isYear(v: unknown): v is number {
  return typeof v === 'number' && Number.isInteger(v) && v >= 1990 && v <= 2200;
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

/** The year a preset belongs to: its `year`, else its Central-time date (an older backend sends no year). */
export function presetYear(p: Pick<Preset, 'year' | 'date_local' | 'start_utc'>): number | null {
  if (isYear(p.year)) return p.year;
  const date = typeof p.date_local === 'string' && /^\d{4}-/.test(p.date_local) ? p.date_local : null;
  const fromDate = date ?? (typeof p.start_utc === 'string' ? localDateOf(p.start_utc) : null);
  const year = fromDate ? Number(fromDate.slice(0, 4)) : Number.NaN;
  return isYear(year) ? year : null;
}

export function readPresets(raw: unknown): Preset[] {
  if (!Array.isArray(raw)) return DOCUMENTED_PRESETS;
  const out = raw.filter(
    (p): p is Preset =>
      isObject(p) && typeof p.id === 'string' && typeof p.label === 'string' && typeof p.start_utc === 'string',
  );
  return out.length ? out : DOCUMENTED_PRESETS;
}

/**
 * Year {year, label, description, real_until_utc}, keeping only years that have presets and are 2025 or
 * later. A year some preset names but the list leaves out still gets a plain entry, so no preset is lost.
 */
export function readYears(raw: unknown, presets: readonly Preset[]): YearOption[] {
  const withPresets = new Set(presets.map(presetYear).filter((y): y is number => y !== null && y >= FIRST_YEAR));
  const out = new Map<number, YearOption>();
  for (const y of Array.isArray(raw) ? raw : []) {
    if (!isObject(y) || !isYear(y.year) || !withPresets.has(y.year) || out.has(y.year)) continue;
    const realUntil = typeof y.real_until_utc === 'string' && Number.isFinite(Date.parse(y.real_until_utc)) ? y.real_until_utc : null;
    out.set(y.year, {
      year: y.year,
      label: typeof y.label === 'string' && y.label ? y.label : String(y.year),
      description: typeof y.description === 'string' ? y.description : '',
      realUntilUtc: realUntil,
    });
  }
  for (const year of withPresets) {
    if (!out.has(year)) {
      const known = Array.isArray(raw) ? undefined : DOCUMENTED_YEARS.find((d) => d.year === year);
      out.set(year, known ?? { year, label: String(year), description: '', realUntilUtc: null });
    }
  }
  return [...out.values()].toSorted((a, b) => a.year - b.year);
}

export function readEditable(raw: unknown): EditableKey[] {
  if (!Array.isArray(raw)) return [...EDITABLE_KEYS];
  const keys = EDITABLE_KEYS.filter((k) => raw.includes(k));
  return keys.length ? keys : [...EDITABLE_KEYS];
}

export function readSettingsResponse(raw: unknown): SettingsModel {
  const obj = isObject(raw) ? raw : {};
  const defaults = isObject(obj.defaults) ? { ...DOCUMENTED_DEFAULTS, ...(obj.defaults as Partial<Settings>) } : DOCUMENTED_DEFAULTS;
  const presets = readPresets(obj.presets);
  return {
    defaults,
    editable: readEditable(obj.editable),
    presets,
    years: readYears(obj.years, presets),
    strategies: readStrategies(obj.strategies),
    speeds: readSpeeds(obj.speeds),
    fallback: !isObject(obj.defaults),
  };
}

export const FALLBACK_SETTINGS: SettingsModel = { ...readSettingsResponse(null), fallback: true };

export function presetById(presets: readonly Preset[], id: string): Preset | undefined {
  return presets.find((p) => p.id === id);
}

/** The presets of one year, in the server's order. */
export function presetsForYear(presets: readonly Preset[], year: number): Preset[] {
  return presets.filter((p) => presetYear(p) === year);
}

export function yearOption(model: Pick<SettingsModel, 'years'>, year: number): YearOption | undefined {
  return model.years.find((y) => y.year === year);
}

/** The preset to offer first for a year: the default when it belongs to that year, else the year's first. */
export function defaultPresetFor(model: Pick<SettingsModel, 'defaults' | 'presets'>, year: number): string | null {
  const own = presetsForYear(model.presets, year);
  if (!own.length) return null;
  return own.some((p) => p.id === model.defaults.preset) ? model.defaults.preset : own[0].id;
}

/** The editor's starting values: the defaults, with the year taken from the default preset when missing. */
export function draftFrom(model: Pick<SettingsModel, 'defaults' | 'presets'>): EditableSettings {
  const d = model.defaults;
  const preset = presetById(model.presets, d.preset);
  const year = isYear(d.year) ? d.year : ((preset ? presetYear(preset) : null) ?? FIRST_YEAR);
  return { year, preset: d.preset, speed_tps: d.speed_tps, strategy: d.strategy, start_charge_level: d.start_charge_level };
}

/** Picking a year keeps the preset if it belongs to that year, else offers that year's first choice. */
export function withYear(draft: EditableSettings, year: number, model: Pick<SettingsModel, 'defaults' | 'presets'>): EditableSettings {
  const preset = presetById(model.presets, draft.preset);
  if (preset && presetYear(preset) === year) return { ...draft, year };
  return { ...draft, year, preset: defaultPresetFor(model, year) ?? draft.preset };
}

/** Picking a preset also picks its year. */
export function withPreset(draft: EditableSettings, presetId: string, model: Pick<SettingsModel, 'presets'>): EditableSettings {
  const preset = presetById(model.presets, presetId);
  return { ...draft, preset: presetId, year: (preset ? presetYear(preset) : null) ?? draft.year };
}

/** Only the keys visitors may change (the server ignores the rest anyway). */
export function editablePart(draft: EditableSettings, editable: EditableKey[]): Partial<EditableSettings> {
  const out: Partial<EditableSettings> = {};
  for (const key of editable) (out as Record<string, unknown>)[key] = draft[key];
  return out;
}
