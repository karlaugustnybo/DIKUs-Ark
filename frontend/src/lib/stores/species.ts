import { writable } from 'svelte/store';
import type { SpeciesSortKey } from '$lib/species-table';

export const tableSearch = writable('');
export const tableSort = writable<SpeciesSortKey>('priority');
export const tableOrder = writable<'asc' | 'desc'>('desc');
export const tablePage = writable(1);
export const tableRedlist = writable<string[]>([]);
export const tableDna = writable<string[]>([]);
export const tableSystems = writable<string[]>([]);

export type SelectedSpecies = { gbif_accepted_id: string; species_name: string };
export type SpeciesHighlightState = {
  status: 'idle' | 'loading' | 'ready' | 'error';
  count: number;
  resolution: 3 | 7;
  message?: string;
};

export const selectedSpecies = writable<SelectedSpecies | null>(null);
export const speciesHighlight = writable<SpeciesHighlightState>({ status: 'idle', count: 0, resolution: 3 });
export const speciesHighlightGradient = writable(false);
