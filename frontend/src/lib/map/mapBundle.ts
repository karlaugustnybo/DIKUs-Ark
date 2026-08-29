import { config } from '$lib/config';

export type ScoreDomain = { min: number; max: number };
export type TileZoomRange = { min: number; max: number };
export type CoarseSnapshotDescriptor = {
  format: 'arrow-ipc-v1';
  schema_version: number;
  resolution: 3;
  cells: number;
  url: string;
  systems: string[];
  metrics: string[];
  boundary_columns: string[];
};

export type MapMetadata = {
  score_domains: Record<string, ScoreDomain>;
  species_normalized_score_domains: Record<string, ScoreDomain>;
  available_resolutions?: Array<3 | 7>;
  complete_resolutions?: Array<3 | 7>;
  res7_delivery?: string;
  tile_layout?: string;
  tile_schema_version?: number;
  jurisdiction_assignment?: string;
  jurisdiction_score_domains?: Record<string, Record<string, ScoreDomain>>;
  boundary_assignment?: string;
  boundary_score_domains?: Record<string, Record<string, Record<string, ScoreDomain>>>;
  boundary_species_normalized_score_domains?: Record<string, Record<string, Record<string, ScoreDomain>>>;
  boundary_tile_properties?: Record<string, string>;
  resolution_tile_ranges?: Record<string, TileZoomRange>;
  res7_coverage_version?: number;
  coarse_snapshot?: CoarseSnapshotDescriptor;
};

type ArrowColumn = { get: (index: number) => unknown };

export type CoarseSnapshot = {
  length: number;
  indices: Uint32Array;
  h3Indexes: string[];
  metric: (prefix: string, name: string, index: number) => number;
  boundaryCodes: (framework: string, index: number) => readonly string[];
};

export type MapBundle = {
  metadata: MapMetadata;
  coarseSnapshot: CoarseSnapshot | null;
  staticBasemap: GeoJSON.FeatureCollection;
};

let metadataPromise: Promise<MapMetadata> | null = null;
let bundlePromise: Promise<MapBundle> | null = null;

async function loadStaticBasemap(): Promise<GeoJSON.FeatureCollection> {
  const response = await fetch('/data/boundaries/admin0.geojson');
  if (!response.ok) throw new Error(`Unable to load the global basemap (${response.status})`);
  return response.json() as Promise<GeoJSON.FeatureCollection>;
}

export function getMapMetadata(): Promise<MapMetadata> {
  if (!metadataPromise) {
    metadataPromise = fetch(config.mapMetadataUrl)
      .then(async (response) => {
        if (!response.ok) throw new Error(`Unable to load map metadata (${response.status})`);
        return response.json() as Promise<MapMetadata>;
      })
      .catch((reason) => {
        metadataPromise = null;
        throw reason;
      });
  }
  return metadataPromise;
}

function stringList(value: unknown): readonly string[] {
  if (Array.isArray(value)) return value.map(String);
  if (value && typeof (value as { toArray?: unknown }).toArray === 'function') {
    return Array.from((value as { toArray: () => Iterable<unknown> }).toArray(), String);
  }
  return [];
}

async function loadCoarseSnapshot(descriptor: CoarseSnapshotDescriptor): Promise<CoarseSnapshot> {
  // Snapshot URLs are emitted by the tile API. Resolve relative descriptors
  // against the metadata endpoint, not the frontend page origin (the Vite
  // development server and API deliberately run on different ports).
  const metadataUrl = new URL(config.mapMetadataUrl, window.location.href);
  const snapshotUrl = new URL(descriptor.url, metadataUrl);
  const [initialResponse, arrow] = await Promise.all([
    fetch(snapshotUrl),
    import('apache-arrow')
  ]);
  // A failed response can outlive a development-server restart in the HTTP
  // cache. Retry that exceptional path from the network; successful hashed
  // artifacts remain cached normally via their immutable response headers.
  const response = initialResponse.ok
    ? initialResponse
    : await fetch(snapshotUrl, { cache: 'reload' });
  if (!response.ok) {
    throw new Error(`Unable to load the global coarse map (${response.status} from ${snapshotUrl})`);
  }
  const table = arrow.tableFromIPC(await response.arrayBuffer());
  const h3Column = table.getChild('h3_index') as ArrowColumn | null;
  if (!h3Column || table.numRows !== descriptor.cells) {
    throw new Error('The global coarse map snapshot does not match its metadata');
  }
  const metricColumns = new Map<string, ArrowColumn>();
  for (const prefix of descriptor.systems) {
    for (const metric of descriptor.metrics) {
      const name = `${prefix}_${metric}`;
      const column = table.getChild(name) as ArrowColumn | null;
      if (!column) throw new Error(`The global coarse map is missing ${name}`);
      metricColumns.set(name, column);
    }
  }
  const boundaryColumns = new Map<string, ArrowColumn>();
  for (const framework of descriptor.boundary_columns) {
    const column = table.getChild(framework) as ArrowColumn | null;
    if (column) boundaryColumns.set(framework, column);
  }
  const h3Indexes = Array.from({ length: table.numRows }, (_, index) => String(h3Column.get(index)));
  return {
    length: table.numRows,
    indices: Uint32Array.from({ length: table.numRows }, (_, index) => index),
    h3Indexes,
    metric: (prefix, name, index) => Number(metricColumns.get(`${prefix}_${name}`)?.get(index) ?? 0),
    boundaryCodes: (framework, index) => stringList(boundaryColumns.get(framework)?.get(index))
  };
}

export function preloadMapBundle(): Promise<MapBundle> {
  if (!bundlePromise) {
    bundlePromise = getMapMetadata()
      .then(async (metadata) => {
        const [coarseSnapshot, staticBasemap] = await Promise.all([
          metadata.coarse_snapshot
            ? loadCoarseSnapshot(metadata.coarse_snapshot)
            : Promise.resolve(null),
          loadStaticBasemap()
        ]);
        return { metadata, coarseSnapshot, staticBasemap };
      })
      .catch((reason) => {
        bundlePromise = null;
        throw reason;
      });
  }
  return bundlePromise;
}

export function preloadMapBundleInIdleTime(): () => void {
  const connection = (navigator as Navigator & {
    connection?: { saveData?: boolean; effectiveType?: string };
  }).connection;
  if (connection?.saveData || connection?.effectiveType === '2g') return () => {};
  const start = () => { void preloadMapBundle().catch(() => {}); };
  if ('requestIdleCallback' in window) {
    const id = window.requestIdleCallback(start, { timeout: 1_500 });
    return () => window.cancelIdleCallback(id);
  }
  const id = globalThis.setTimeout(start, 400);
  return () => globalThis.clearTimeout(id);
}
