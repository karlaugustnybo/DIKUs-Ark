import { config } from '$lib/config';
import type { components } from './schema';

export type StatsResponse = components['schemas']['StatsResponse'];
export type SpeciesPage = components['schemas']['SpeciesPage'];
export type SpeciesRow = components['schemas']['SpeciesRow'];
export type SpeciesSuggestion = components['schemas']['SpeciesSuggestion'];
export type SpeciesSuggestions = components['schemas']['SpeciesSuggestions'];
export type SpeciesCellsResponse = components['schemas']['SpeciesCellsResponse'];
export type CellDetailsResponse = components['schemas']['CellDetailsResponse'];
export type CellSpeciesRow = components['schemas']['CellSpeciesRow'];

async function get<T>(path: string, params?: URLSearchParams, options?: RequestInit): Promise<T> {
  const suffix = params?.size ? `?${params}` : '';
  const response = await fetch(`${config.apiBaseUrl}${path}${suffix}`, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body: unknown = await response.json();
      if (typeof body === 'object' && body !== null && 'detail' in body && typeof body.detail === 'string') {
        message = body.detail;
      }
    } catch {
      // Keep the status text when the upstream response is not JSON.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export const api = {
  stats: () => get<StatsResponse>('/api/stats'),
  species: (params: URLSearchParams, signal?: AbortSignal) =>
    get<SpeciesPage>('/api/species', params, { signal }),
  speciesSuggestions: (search: string, limit = 8, signal?: AbortSignal) =>
    get<SpeciesSuggestions>(
      '/api/species/suggestions',
      new URLSearchParams({ search, limit: String(limit) }),
      { signal }
    ),
  speciesCells: (gbifAcceptedId: string, resolution: number, signal?: AbortSignal) =>
    get<SpeciesCellsResponse>(
      `/api/species/${encodeURIComponent(gbifAcceptedId)}/cells`,
      new URLSearchParams({ resolution: String(resolution) }),
      { signal }
    ),
  cell: (h3Index: string, params: URLSearchParams, signal?: AbortSignal) =>
    get<CellDetailsResponse>(
      `/api/cells/${encodeURIComponent(h3Index)}/species`, params, { signal }
    )
};
