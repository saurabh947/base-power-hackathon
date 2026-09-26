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
  it('accepts the likely Choice shapes for speeds and labels them in plain words', () => {
    expect(readSpeeds([1, 2, 5, 10]).map((s) => s.value)).toEqual([1, 2, 5, 10]);
    expect(readSpeeds([{ value: 5, label: 'x' }, { speed_tps: 2 }, { id: '10' }]).map((s) => s.value)).toEqual([2, 5, 10]);
    expect(readSpeeds([{ value: 2 }])[0].label).toBe('1 hour of market time every 6 seconds');
    expect(readSpeeds('nonsense').map((s) => s.value)).toEqual([1, 2, 5, 10]);
  });

  it('prefers server labels for strategies and falls back to the glossary', () => {
    const list = readStrategies(['bands', { value: 'forecast', label: 'AI forecast (TimesFM 2.5)' }, { id: 'peak' }]);
    expect(list.map((s) => s.label)).toEqual(['Simple rule', 'AI forecast (TimesFM 2.5)', 'Evening peak']);
    expect(list.every((s) => s.description.length > 0)).toBe(true);
    expect(readStrategies(undefined)).toHaveLength(3);
  });

  it('keeps only valid presets and editable keys', () => {
    expect(readPresets([{ id: 'a', label: 'A' }, { id: 3 }])).toEqual([{ id: 'a', label: 'A' }]);
    expect(readEditable(['preset', 'fleet_size', 'strategy'])).toEqual(['preset', 'strategy']);
    expect(readEditable(null)).toEqual(['preset', 'speed_tps', 'strategy', 'start_charge_level']);
  });

  it('falls back to the documented defaults when the endpoint fails', () => {
    const model = readSettingsResponse(null);
    expect(model.fallback).toBe(true);
    expect(model.defaults).toEqual(DOCUMENTED_DEFAULTS);
    expect(model.presets[0].id).toBe('spike_evening');
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
