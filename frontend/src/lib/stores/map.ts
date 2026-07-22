import { writable } from 'svelte/store';
import type { CellDetailsResponse } from '$lib/api/client';

export type HabitatSystem = '' | 'Terrestrial' | 'Freshwater' | 'Marine';
export const habitatSystem = writable<HabitatSystem>('');
export const resolution = writable<3 | 7>(3);
export const selectedH3 = writable<string | null>(null);
export const cellDetails = writable<CellDetailsResponse | null>(null);
export const cellLoading = writable(false);
