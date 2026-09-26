// The fleet's cities. docs/API.md: "City names shown to people come from
// GET /fleet/layout cities[].name". Until the layout arrives (or if it cannot),
// the documented Texas Triangle is used: the same locations, names, counts and
// battery id blocks as config.yaml.

import { createStore, useStore } from './store.ts';

export interface City {
  /** Load zone id, e.g. "LZ_NORTH". */
  location: string;
  /** What people see, e.g. "Dallas–Fort Worth". */
  name: string;
  /** Short form for tight spaces, e.g. "DFW". */
  short: string;
  batteries: number;
  /** Index of the city's first battery: ids run in contiguous blocks. */
  firstBattery: number;
  /** [longitude, latitude] of the metro centre. */
  center: [number, number];
}

/** docs/API.md, in config (battery id) order. */
export const DEFAULT_CITIES: readonly City[] = Object.freeze([
  { location: 'LZ_HOUSTON', name: 'Houston', short: 'HOU', batteries: 7000, firstBattery: 0, center: [-95.37, 29.76] },
  { location: 'LZ_NORTH', name: 'Dallas–Fort Worth', short: 'DFW', batteries: 8000, firstBattery: 7000, center: [-96.97, 32.82] },
  { location: 'LZ_CPS', name: 'San Antonio', short: 'SA', batteries: 2600, firstBattery: 15000, center: [-98.49, 29.42] },
  { location: 'LZ_AEN', name: 'Austin', short: 'ATX', batteries: 2400, firstBattery: 17600, center: [-97.74, 30.27] },
] satisfies City[]);

const FALLBACK_NAMES: Record<string, string> = Object.fromEntries(DEFAULT_CITIES.map((c) => [c.location, c.name]));

export const citiesStore = createStore<readonly City[]>(DEFAULT_CITIES);

/** Called when GET /fleet/layout arrives; keeps the same array when nothing changed. */
export function setCities(next: readonly City[]): void {
  if (!next.length) return;
  citiesStore.set((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
}

export function useCities(): readonly City[] {
  return useStore(citiesStore, (c) => c);
}

export function cityOf(location: string, list: readonly City[] = citiesStore.get()): City | undefined {
  return list.find((c) => c.location === location);
}

/** "Dallas–Fort Worth" for LZ_NORTH; the id itself for anything unknown. */
export function cityName(location: string): string {
  return cityOf(location)?.name ?? FALLBACK_NAMES[location] ?? location;
}

/** Biggest first: Dallas–Fort Worth, Houston, San Antonio, Austin (the order people are shown). */
export function byBatteries(list: readonly City[]): City[] {
  return list.toSorted((a, b) => b.batteries - a.batteries || a.firstBattery - b.firstBattery);
}

/** The display order of location ids, biggest first. */
export function displayLocations(list: readonly City[] = citiesStore.get()): string[] {
  return byBatteries(list).map((c) => c.location);
}
