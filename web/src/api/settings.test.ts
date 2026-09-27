import { describe, expect, it } from 'vitest';
import { resolveApiBase, toWebSocketUrl } from './config.ts';
import type { Preset } from './types.ts';
import {
  defaultPresetFor,
  DOCUMENTED_DEFAULTS,
  draftFrom,
  editablePart,
  presetsForYear,
  presetYear,
  readEditable,
  readPresets,
  readSettingsResponse,
  readSpeeds,
  readStrategies,
  readYears,
  withPreset,
  withYear,
} from './settings.ts';

describe('GET /settings readers', () => {
  it('reads Speed {tps, label} and words it the site\'s way', () => {
    const speeds = readSpeeds([
      { tps: 5, label: '1 hour every 2.4 s' },
      { tps: 2, label: '1 hour every 6 s' },
      { tps: 2, label: 'duplicate' },
    ]);
    expect(speeds).toEqual([
      { value: 2, label: '1 hour of market time every 6 seconds' },
      { value: 5, label: '1 hour of market time every 2.4 seconds' },
    ]);
    expect(readSpeeds([{ label: 'no tps' }, 'nonsense']).map((s) => s.value)).toEqual([1, 2, 5, 10]);
    expect(readSpeeds(undefined).map((s) => s.value)).toEqual([1, 2, 5, 10]);
  });

  it('reads Strategy {id, label, description}, with the glossary as a fallback', () => {
    const list = readStrategies([
      { id: 'bands', label: 'Simple rule', description: 'From the server.' },
      { id: 'forecast', label: 'AI forecast (TimesFM 2.5)' },
      { id: 'peak', description: 'Evening.' },
      { label: 'no id' },
    ]);
    expect(list.map((s) => [s.value, s.label])).toEqual([
      ['bands', 'Simple rule'],
      ['forecast', 'AI forecast (TimesFM 2.5)'],
      ['peak', 'Evening peak'],
    ]);
    expect(list[0].description).toBe('From the server.');
    expect(list[1].description.length).toBeGreaterThan(0);
    expect(readStrategies(undefined)).toHaveLength(3);
  });

  it('keeps presets that have an id, label and start_utc, and only editable keys', () => {
    const good = {
      id: 'summer_spike',
      label: 'Summer night spike',
      date_local: '2025-07-11',
      start_utc: '2025-07-11T23:00:00Z',
      start_local: '2025-07-11T18:00:00-05:00',
      description: 'Both areas jump to about $1,750/MWh at 9 pm.',
    };
    expect(readPresets([good, { id: 'a', label: 'A' }, { id: 3 }])).toEqual([good]);
    expect(readPresets([{ id: 'a' }])[0].id).toBe('morning_spike'); // documented fallback
    expect(readEditable(['preset', 'fleet_size', 'strategy'])).toEqual(['preset', 'strategy']);
    expect(readEditable(null)).toEqual(['year', 'preset', 'speed_tps', 'strategy', 'start_charge_level']);
    expect(readEditable(['year', 'preset'])).toEqual(['year', 'preset']);
  });

  it('falls back to the documented defaults when the endpoint fails', () => {
    const model = readSettingsResponse(null);
    expect(model.fallback).toBe(true);
    expect(model.defaults).toEqual(DOCUMENTED_DEFAULTS);
    expect(model.presets[0].id).toBe('morning_spike');
    const real = readSettingsResponse({ defaults: { ...DOCUMENTED_DEFAULTS, speed_tps: 5 } });
    expect(real.fallback).toBe(false);
    expect(real.defaults.speed_tps).toBe(5);
  });

  it('sends only the editable keys', () => {
    const draft = { year: 2025, preset: 'spike_evening', speed_tps: 5, strategy: 'bands', start_charge_level: 0.5 };
    expect(editablePart(draft, ['preset', 'speed_tps'])).toEqual({ preset: 'spike_evening', speed_tps: 5 });
    expect(editablePart(draft, ['year', 'preset'])).toEqual({ year: 2025, preset: 'spike_evening' });
  });
});

const preset = (id: string, year: number | undefined, date: string, label = id): Preset => ({
  id,
  ...(year === undefined ? {} : { year }),
  label,
  date_local: date,
  start_utc: `${date}T23:00:00Z`,
  description: '',
});

/** docs/API.md: presets carry their year; each year has a "From January 1". */
const PRESETS: Preset[] = [
  preset('spike_evening', 2025, '2025-05-16'),
  preset('ai_best_evening', 2025, '2025-05-30'),
  preset('jan1_2025', 2025, '2025-01-01', 'From January 1'),
  preset('predicted_2026', 2026, '2026-09-20'),
  preset('jan1_2026', 2026, '2026-01-01', 'From January 1'),
];

/** A preset as an older backend sends it, without its year. */
const withoutYear = (p: Preset) => Object.fromEntries(Object.entries(p).filter(([k]) => k !== 'year'));

const YEARS = [
  { year: 2025, label: '2025', description: 'Real ERCOT prices for the whole year.', real_until_utc: null },
  { year: 2026, label: '2026', description: 'Real prices through Sep 20, then the AI’s prediction.', real_until_utc: '2026-09-21T05:00:00Z' },
];

describe('years and presets', () => {
  it('reads Year {year, label, description, real_until_utc}, oldest first', () => {
    expect(readYears(YEARS.toReversed(), PRESETS)).toEqual([
      { year: 2025, label: '2025', description: 'Real ERCOT prices for the whole year.', realUntilUtc: null },
      { year: 2026, label: '2026', description: 'Real prices through Sep 20, then the AI’s prediction.', realUntilUtc: '2026-09-21T05:00:00Z' },
    ]);
  });

  it('offers only 2025 and later (no 2024), and only years that have presets', () => {
    const with2024 = [...PRESETS, preset('old', 2024, '2024-08-20')];
    const years2024 = [{ year: 2024, label: '2024', description: '', real_until_utc: null }, ...YEARS];
    expect(readYears(years2024, with2024).map((y) => y.year)).toEqual([2025, 2026]);
    expect(readYears(YEARS, PRESETS.filter((p) => p.year === 2025)).map((y) => y.year)).toEqual([2025]);
    // A year a preset names but the list leaves out still gets a plain entry.
    expect(readYears([YEARS[0]], PRESETS).map((y) => [y.year, y.label, y.realUntilUtc])).toEqual([
      [2025, '2025', null],
      [2026, '2026', null],
    ]);
  });

  it('knows a preset’s year from its `year`, else from its date (an older backend)', () => {
    expect(presetYear(preset('x', 2026, '2025-12-31'))).toBe(2026);
    expect(presetYear(preset('x', undefined, '2025-05-16'))).toBe(2025);
    expect(presetYear({ date_local: '', start_utc: '2026-01-01T06:00:00Z' })).toBe(2026); // midnight Central time
    // An older backend: no years, no preset years; everything is 2025, with the documented wording.
    const old = readSettingsResponse({ defaults: { ...DOCUMENTED_DEFAULTS, year: undefined }, presets: PRESETS.slice(0, 2).map(withoutYear) });
    expect(old.years).toEqual([{ year: 2025, label: '2025', description: 'Real ERCOT prices for the whole year.', realUntilUtc: null }]);
    expect(draftFrom(old).year).toBe(2025);
  });

  it('filters the presets by year, each year with its "From January 1"', () => {
    expect(presetsForYear(PRESETS, 2025).map((p) => p.id)).toEqual(['spike_evening', 'ai_best_evening', 'jan1_2025']);
    expect(presetsForYear(PRESETS, 2026).map((p) => p.id)).toEqual(['predicted_2026', 'jan1_2026']);
    for (const y of [2025, 2026]) expect(presetsForYear(PRESETS, y).some((p) => p.label === 'From January 1')).toBe(true);
    expect(presetsForYear(PRESETS, 2024)).toEqual([]);
  });

  it('picking a year offers that year’s presets; picking a preset picks its year', () => {
    const model = { defaults: { ...DOCUMENTED_DEFAULTS, preset: 'ai_best_evening' }, presets: PRESETS };
    const draft = draftFrom(model);
    expect(draft).toMatchObject({ year: 2025, preset: 'ai_best_evening' });
    const in2026 = withYear(draft, 2026, model);
    expect(in2026).toMatchObject({ year: 2026, preset: 'predicted_2026' }); // that year's first
    expect(withYear(in2026, 2025, model)).toMatchObject({ year: 2025, preset: 'ai_best_evening' }); // back to the default
    expect(withYear({ ...draft, preset: 'jan1_2025' }, 2025, model).preset).toBe('jan1_2025'); // kept: it is 2025's
    expect(withPreset(draft, 'jan1_2026', model)).toMatchObject({ year: 2026, preset: 'jan1_2026' });
    expect(defaultPresetFor(model, 2026)).toBe('predicted_2026');
    expect(defaultPresetFor(model, 2024)).toBeNull();
  });

  it('reads a full GET /settings with years, and falls back to both documented years', () => {
    const model = readSettingsResponse({ defaults: DOCUMENTED_DEFAULTS, presets: PRESETS, years: YEARS });
    expect(model.years.map((y) => y.year)).toEqual([2025, 2026]);
    expect(model.years[1].description).toBe('Real prices through Sep 20, then the AI’s prediction.');
    const fallback = readSettingsResponse(null);
    expect(fallback.years.map((y) => [y.year, y.realUntilUtc])).toEqual([
      [2025, null],
      [2026, '2026-09-21T05:00:00Z'],
    ]);
    expect(presetsForYear(fallback.presets, 2026).map((p) => p.label)).toEqual(['Biggest evening spike of 2026', 'From January 1']);
  });
});

describe('backend URL', () => {
  it('normalises VITE_API_BASE_URL', () => {
    expect(resolveApiBase('https://fleet-sim-abc.a.run.app/', 'https://site.vercel.app', null)).toBe(
      'https://fleet-sim-abc.a.run.app',
    );
    expect(resolveApiBase('  ', 'http://localhost:5173', 'http://localhost:5173/__mock')).toBe(
      'http://localhost:5173/__mock',
    );
    expect(resolveApiBase(undefined, 'https://site.vercel.app', null)).toBeNull();
    expect(resolveApiBase('/api', 'https://site.vercel.app', null)).toBe('https://site.vercel.app/api');
  });

  it('derives the WebSocket URL', () => {
    expect(toWebSocketUrl('https://fleet-sim-abc.a.run.app')).toBe('wss://fleet-sim-abc.a.run.app/live');
    expect(toWebSocketUrl('http://localhost:8000')).toBe('ws://localhost:8000/live');
    expect(toWebSocketUrl('http://localhost:5173/__mock')).toBe('ws://localhost:5173/__mock/live');
  });
});
