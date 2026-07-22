import { writable } from 'svelte/store';

export type SortKey = 'species_name' | 'family' | 'redlist_category' | 'threat_score' | 'dna_level' | 'priority';
export const tableSearch = writable('');
export const tableSort = writable<SortKey>('priority');
export const tableOrder = writable<'asc' | 'desc'>('desc');
export const tablePage = writable(1);
