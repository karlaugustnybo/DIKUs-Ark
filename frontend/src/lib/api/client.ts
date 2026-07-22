import { config } from '$lib/config';
import type { components } from './schema';

export type StatsResponse = components['schemas']['StatsResponse'];
export type SpeciesPage = components['schemas']['SpeciesPage'];
export type SpeciesRow = components['schemas']['SpeciesRow'];
export type CellDetailsResponse = components['schemas']['CellDetailsResponse'];
export type CellSpeciesRow = components['schemas']['CellSpeciesRow'];

async function get<T>(path: string, params?: URLSearchParams): Promise<T> {
  const suffix = params?.size ? `?${params}` : '';
  const response = await fetch(`${config.apiBaseUrl}${path}${suffix}`);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json() as Promise<T>;
}

export const api = {
  stats: () => get<StatsResponse>('/api/stats'),
  species: (params: URLSearchParams) => get<SpeciesPage>('/api/species', params),
  cell: (h3Index: string, params: URLSearchParams) =>
    get<CellDetailsResponse>(`/api/cells/${encodeURIComponent(h3Index)}/species`, params)
};
