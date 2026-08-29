import { writable } from 'svelte/store';
import type { CellDetailsResponse } from '$lib/api/client';
import type { ColourPaletteId } from '$lib/map/colourPalettes';

export type HabitatSystem = '' | 'Terrestrial' | 'Freshwater' | 'Marine';
export const habitatSystem = writable<HabitatSystem>('');
export const resolution = writable<3 | 7>(3);
export const normalizeColoursBySpecies = writable(false);
export const colourPalette = writable<ColourPaletteId>('turbo');
export const selectedH3 = writable<string | null>(null);
export const selectedH3Resolution = writable<3 | 7 | null>(null);
export const cellDetails = writable<CellDetailsResponse | null>(null);
export const cellLoading = writable(false);
