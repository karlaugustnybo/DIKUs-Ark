import { get, writable } from 'svelte/store';

export type BoundaryFramework = {
  id: string;
  name: string;
  short_name: string;
  description: string;
  group: string;
  relationship: 'single' | 'many';
  status: 'ready' | 'source-required';
  filterable?: boolean;
  tile_property?: string;
  catalog_url?: string;
  catalog_partition_url?: string;
  data_url?: string;
  parent_framework?: string;
  source_url?: string;
  license?: string;
  coverage_note?: string;
  available_parent_codes?: string[];
  import_hint?: string;
  color: [number, number, number];
};

export type BoundaryProperties = {
  code: string;
  name: string;
  parent_code?: string;
  continent?: string;
  region?: string | null;
  boundary_type?: string | null;
  biome?: string | null;
  conservation_status?: string | null;
  geometry_url?: string;
};

export type BoundaryFeature = {
  type: 'Feature';
  properties: BoundaryProperties;
  geometry: { type: string; coordinates: unknown } | null;
};

export type BoundaryCollection = {
  type: 'FeatureCollection';
  features: BoundaryFeature[];
  framework?: string;
  source?: string;
  source_version?: string;
};

export const boundaryFrameworks = writable<BoundaryFramework[]>([]);
export const boundaryCollections = writable<Record<string, BoundaryCollection>>({});
export const boundaryFilters = writable<Record<string, string[]>>({});
export const boundaryLoadError = writable('');

let manifestPromise: Promise<BoundaryFramework[]> | null = null;
const collectionPromises = new Map<string, Promise<BoundaryCollection>>();

export function loadBoundaryFrameworks(): Promise<BoundaryFramework[]> {
  if (manifestPromise) return manifestPromise;
  manifestPromise = fetch('/data/boundary-frameworks.json')
    .then(async (response) => {
      if (!response.ok) throw new Error(`Unable to load boundary frameworks (${response.status})`);
      const data = await response.json() as { frameworks: BoundaryFramework[] };
      // Local global-data installations supply a small descriptor alongside
      // country catalogues. Fresh code-only checkouts retain the bundled preview.
      const adm2 = await fetch('/data/adm2-catalogs/framework.json');
      if (adm2.ok && adm2.headers.get('content-type')?.includes('application/json')) {
        const globalFramework = await adm2.json() as BoundaryFramework;
        if (globalFramework.id !== 'municipality' || !globalFramework.catalog_partition_url) {
          throw new Error('Invalid global ADM2 catalogue descriptor');
        }
        data.frameworks = data.frameworks.map((framework) => framework.id === 'municipality'
          ? globalFramework : framework);
      } else if (!adm2.ok && adm2.status !== 404) {
        throw new Error(`Unable to load global ADM2 coverage (${adm2.status})`);
      }
      boundaryFrameworks.set(data.frameworks);
      return data.frameworks;
    })
    .catch((reason) => {
      manifestPromise = null;
      boundaryLoadError.set(reason instanceof Error ? reason.message : 'Unable to load boundary frameworks');
      throw reason;
    });
  return manifestPromise;
}

function mergeCollection(frameworkId: string, incoming: BoundaryCollection): BoundaryCollection {
  const current = get(boundaryCollections)[frameworkId];
  const features = new Map<string, BoundaryFeature>();
  for (const feature of current?.features ?? []) features.set(feature.properties.code, feature);
  for (const feature of incoming.features) features.set(feature.properties.code, feature);
  const collection: BoundaryCollection = {
    ...incoming,
    type: 'FeatureCollection',
    features: [...features.values()].sort((left, right) =>
      (left.properties.name ?? left.properties.code).localeCompare(
        right.properties.name ?? right.properties.code
      )
    )
  };
  boundaryCollections.update((collections) => ({
    ...collections,
    [frameworkId]: collection
  }));
  return collection;
}

async function fetchBoundaryCatalogue(
  frameworkId: string,
  framework: BoundaryFramework,
  url: string
): Promise<BoundaryCollection> {
  const cacheKey = `${frameworkId}:${url}`;
  const existing = collectionPromises.get(cacheKey);
  if (existing) return existing;
  const pending = fetch(url).then(async (response) => {
    if (!response.ok) throw new Error(`Unable to load ${framework.name} (${response.status})`);
    const payload = await response.json() as BoundaryCollection | { features: BoundaryProperties[] };
    const collection: BoundaryCollection = {
      ...payload,
      type: 'FeatureCollection',
      features: payload.features.map((feature) => 'properties' in feature
        ? feature as BoundaryFeature
        : { type: 'Feature', properties: feature, geometry: null })
    };
    boundaryLoadError.set('');
    return mergeCollection(frameworkId, collection);
  }).catch((reason) => {
    collectionPromises.delete(cacheKey);
    boundaryLoadError.set(reason instanceof Error ? reason.message : `Unable to load ${framework.name}`);
    throw reason;
  });
  collectionPromises.set(cacheKey, pending);
  return pending;
}

export async function loadBoundaryCollection(
  frameworkId: string,
  parentCodes: string[] = []
): Promise<BoundaryCollection> {
  const frameworks = await loadBoundaryFrameworks();
  const framework = frameworks.find((item) => item.id === frameworkId);
  if (!framework) throw new Error(`Unknown boundary framework: ${frameworkId}`);
  const uniqueParents = [...new Set(parentCodes)];
  const urls = uniqueParents.length && framework.catalog_partition_url
    ? uniqueParents.map((parent) => framework.catalog_partition_url!.replace(
        '{parent}', encodeURIComponent(parent.toLocaleLowerCase())
      ))
    : [framework.catalog_url ?? framework.data_url].filter(Boolean) as string[];
  if (!urls.length) throw new Error(`${framework.name} has no configured data source`);
  await Promise.all(urls.map((url) => fetchBoundaryCatalogue(frameworkId, framework, url)));
  return get(boundaryCollections)[frameworkId];
}

export function toggleBoundary(frameworkId: string, code: string, maximum = 30) {
  boundaryFilters.update((filters) => {
    const selected = filters[frameworkId] ?? [];
    const next = selected.includes(code)
      ? selected.filter((item) => item !== code)
      : selected.length < maximum ? [...selected, code] : selected;
    const result = { ...filters, [frameworkId]: next };
    if (!next.length) delete result[frameworkId];
    return result;
  });
}

export function clearBoundaryFilters(frameworkId?: string) {
  if (!frameworkId) boundaryFilters.set({});
  else boundaryFilters.update((filters) => {
    const result = { ...filters };
    delete result[frameworkId];
    return result;
  });
}
