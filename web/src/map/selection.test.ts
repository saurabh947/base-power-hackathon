import { describe, expect, it } from 'vitest';
import { dispatchSelection, escapeClears, nextSelection, onSelectionChange, selectionStore } from './selection.ts';

const CITIES = ['LZ_HOUSTON', 'LZ_NORTH', 'LZ_CPS', 'LZ_AEN'];

const target = (tagName: string, inDialog = false) =>
  ({ tagName, isContentEditable: false, closest: (s: string) => (inDialog && s === 'dialog[open]' ? {} : null) }) as unknown as EventTarget;

describe('city selection', () => {
  it('selects a city from its label, and goes back to all of Texas when pressed again', () => {
    expect(nextSelection(null, { type: 'label', location: 'LZ_NORTH' }, CITIES)).toBe('LZ_NORTH');
    expect(nextSelection('LZ_NORTH', { type: 'label', location: 'LZ_AEN' }, CITIES)).toBe('LZ_AEN');
    expect(nextSelection('LZ_AEN', { type: 'label', location: 'LZ_AEN' }, CITIES)).toBeNull();
  });

  it("selects a city by clicking its dots; empty map, the All of Texas chip and Esc clear it", () => {
    expect(nextSelection(null, { type: 'map', location: 'LZ_CPS' }, CITIES)).toBe('LZ_CPS');
    expect(nextSelection('LZ_CPS', { type: 'map', location: 'LZ_CPS' }, CITIES)).toBe('LZ_CPS'); // clicking it again keeps it
    expect(nextSelection('LZ_CPS', { type: 'map', location: null }, CITIES)).toBeNull();
    expect(nextSelection('LZ_CPS', { type: 'all' }, CITIES)).toBeNull();
    expect(nextSelection('LZ_CPS', { type: 'escape' }, CITIES)).toBeNull();
  });

  it('ignores cities the layout does not have', () => {
    expect(nextSelection(null, { type: 'label', location: 'HB_WEST' }, CITIES)).toBeNull();
    expect(nextSelection('LZ_NORTH', { type: 'map', location: 'HB_WEST' }, CITIES)).toBe('LZ_NORTH');
  });

  it('stores the selection and tells a listener only when it changes', () => {
    const seen: (string | null)[] = [];
    onSelectionChange((loc) => seen.push(loc));
    selectionStore.set(null);
    dispatchSelection({ type: 'label', location: 'LZ_HOUSTON' }, CITIES);
    dispatchSelection({ type: 'map', location: 'LZ_HOUSTON' }, CITIES);
    dispatchSelection({ type: 'escape' }, CITIES);
    expect(seen).toEqual(['LZ_HOUSTON', null]);
    expect(selectionStore.get()).toBeNull();
    onSelectionChange(null);
  });

  it('leaves Esc to dialogs and form fields', () => {
    expect(escapeClears({ key: 'Escape', defaultPrevented: false, target: target('BUTTON') })).toBe(true);
    expect(escapeClears({ key: 'Escape', defaultPrevented: false, target: null })).toBe(true);
    expect(escapeClears({ key: 'Enter', defaultPrevented: false, target: target('BUTTON') })).toBe(false);
    expect(escapeClears({ key: 'Escape', defaultPrevented: true, target: target('BUTTON') })).toBe(false);
    expect(escapeClears({ key: 'Escape', defaultPrevented: false, target: target('SELECT') })).toBe(false);
    expect(escapeClears({ key: 'Escape', defaultPrevented: false, target: target('INPUT') })).toBe(false);
    expect(escapeClears({ key: 'Escape', defaultPrevented: false, target: target('BUTTON', true) })).toBe(false);
  });
});
