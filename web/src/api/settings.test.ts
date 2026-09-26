import { describe, expect, it } from 'vitest';
import { resolveApiBase, toWebSocketUrl } from './config.ts';
import {
  DOCUMENTED_DEFAULTS,
  editablePart,
  readEditable,
  readPresets,
  readSettingsResponse,
  readSpeeds,
  readStrategies,
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
    expect(readPresets([{ id: 'a' }])[0].id).toBe('ai_best_evening'); // documented fallback
    expect(readEditable(['preset', 'fleet_size', 'strategy'])).toEqual(['preset', 'strategy']);
    expect(readEditable(null)).toEqual(['preset', 'speed_tps', 'strategy', 'start_charge_level']);
  });

  it('falls back to the documented defaults when the endpoint fails', () => {
    const model = readSettingsResponse(null);
    expect(model.fallback).toBe(true);
    expect(model.defaults).toEqual(DOCUMENTED_DEFAULTS);
    expect(model.presets[0].id).toBe('ai_best_evening');
    const real = readSettingsResponse({ defaults: { ...DOCUMENTED_DEFAULTS, speed_tps: 5 } });
    expect(real.fallback).toBe(false);
    expect(real.defaults.speed_tps).toBe(5);
  });

  it('sends only the editable keys', () => {
    const draft = { preset: 'spike_evening', speed_tps: 5, strategy: 'bands', start_charge_level: 0.5 };
    expect(editablePart(draft, ['preset', 'speed_tps'])).toEqual({ preset: 'spike_evening', speed_tps: 5 });
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
