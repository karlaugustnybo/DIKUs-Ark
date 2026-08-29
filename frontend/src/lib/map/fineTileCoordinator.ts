import type { CompactCell, CompactTile } from './compactTiles';

export type FineTileIndex = { z: number; x: number; y: number };

export type FineTileCamera = {
  longitude: number;
  latitude: number;
  zoom: number;
  width: number;
  height: number;
  bounds?: {
    west: number;
    south: number;
    east: number;
    north: number;
  };
  corners?: Array<{ longitude: number; latitude: number }>;
  footprintZoom?: number;
};

export type FineTileSource = {
  key: string;
  minZoom: number;
  maxZoom: number;
  load: (index: FineTileIndex) => Promise<CompactTile>;
};

export type FineTileCommit = {
  chunks: FineTileChunk[];
  cellCount: number;
  sourceKey: string;
  tileCount: number;
  tileKeys: ReadonlySet<string>;
};

export type FineTileChunk = {
  key: string;
  cells: CompactCell[];
};

type CoordinatorOptions = {
  guardTiles?: number;
  debounceMs?: number;
  maxCachedTiles?: number;
  onCommit: (commit: FineTileCommit) => void;
  onLoadingChange?: (loading: boolean) => void;
  onError?: (reason: unknown) => void;
};

const WEB_MERCATOR_LATITUDE_LIMIT = 85.051129;

function mercatorY(latitude: number, scale: number): number {
  const bounded = Math.min(
    WEB_MERCATOR_LATITUDE_LIMIT,
    Math.max(-WEB_MERCATOR_LATITUDE_LIMIT, latitude)
  );
  const radians = bounded * Math.PI / 180;
  return (0.5 - Math.log((1 + Math.sin(radians)) /
    (1 - Math.sin(radians))) / (4 * Math.PI)) * scale;
}

function canonicalX(x: number, scale: number): number {
  return ((x % scale) + scale) % scale;
}

/**
 * Return the visible web tiles plus a guard ring. X is canonicalized so a
 * viewport crossing the antimeridian fetches each physical tile only once.
 */
export function fineTileIndices(
  camera: FineTileCamera,
  minZoom: number,
  maxZoom: number,
  guardTiles = 2
): FineTileIndex[] {
  const z = Math.min(maxZoom, Math.max(minZoom, Math.round(camera.zoom + 1)));
  const scale = 2 ** z;
  if (camera.corners && camera.corners.length >= 3) {
    const centerX = (camera.longitude + 180) / 360 * scale;
    const centerY = mercatorY(camera.latitude, scale);
    const footprintScale = 2 ** ((camera.footprintZoom ?? camera.zoom) - camera.zoom);
    const points = camera.corners.map((corner) => {
      let longitude = corner.longitude;
      while (longitude - camera.longitude > 180) longitude -= 360;
      while (longitude - camera.longitude < -180) longitude += 360;
      const x = (longitude + 180) / 360 * scale;
      const y = mercatorY(corner.latitude, scale);
      return {
        x: centerX + (x - centerX) * footprintScale,
        y: centerY + (y - centerY) * footprintScale
      };
    });
    const visible = new Map<string, FineTileIndex>();
    const firstRow = Math.max(0, Math.floor(Math.min(...points.map(({ y }) => y))));
    const lastRow = Math.min(scale - 1, Math.floor(Math.max(...points.map(({ y }) => y))));
    for (let y = firstRow; y <= lastRow; y += 1) {
      const intersections: number[] = [];
      for (let index = 0; index < points.length; index += 1) {
        const start = points[index];
        const end = points[(index + 1) % points.length];
        if (start.y >= y && start.y <= y + 1) intersections.push(start.x);
        for (const scanY of [y, y + 1]) {
          if ((start.y <= scanY && end.y >= scanY) ||
            (end.y <= scanY && start.y >= scanY)) {
            if (start.y === end.y) {
              intersections.push(start.x, end.x);
            } else {
              const progress = (scanY - start.y) / (end.y - start.y);
              intersections.push(start.x + (end.x - start.x) * progress);
            }
          }
        }
      }
      if (!intersections.length) continue;
      const firstColumn = Math.floor(Math.min(...intersections));
      const lastColumn = Math.floor(Math.max(...intersections));
      for (let x = firstColumn; x <= lastColumn; x += 1) {
        const canonical = canonicalX(x, scale);
        visible.set(`${z}/${canonical}/${y}`, { z, x: canonical, y });
      }
    }
    if (guardTiles === 0) return [...visible.values()];
    const guarded = new Map(visible);
    for (const index of visible.values()) {
      for (let dx = -guardTiles; dx <= guardTiles; dx += 1) {
        for (let dy = -guardTiles; dy <= guardTiles; dy += 1) {
          const y = index.y + dy;
          if (y < 0 || y >= scale) continue;
          const x = canonicalX(index.x + dx, scale);
          guarded.set(`${z}/${x}/${y}`, { z, x, y });
        }
      }
    }
    return [...guarded.values()];
  }
  let minX: number;
  let maxX: number;
  let minY: number;
  let maxY: number;
  if (camera.bounds) {
    let west = camera.bounds.west;
    let east = camera.bounds.east;
    while (east < west) east += 360;
    if (east - west > 360) {
      west = camera.longitude - 180;
      east = camera.longitude + 180;
    }
    minX = Math.floor((west + 180) / 360 * scale) - guardTiles;
    maxX = Math.floor((east + 180) / 360 * scale) + guardTiles;
    minY = Math.max(0, Math.floor(mercatorY(camera.bounds.north, scale)) - guardTiles);
    maxY = Math.min(scale - 1, Math.floor(mercatorY(camera.bounds.south, scale)) + guardTiles);
  } else {
    const centerX = (camera.longitude + 180) / 360 * scale;
    const centerY = mercatorY(camera.latitude, scale);
    const tilePixels = 512 * 2 ** (camera.zoom - z);
    const halfWidth = camera.width / (2 * tilePixels);
    const halfHeight = camera.height / (2 * tilePixels);
    minX = Math.floor(centerX - halfWidth) - guardTiles;
    maxX = Math.floor(centerX + halfWidth) + guardTiles;
    minY = Math.max(0, Math.floor(centerY - halfHeight) - guardTiles);
    maxY = Math.min(scale - 1, Math.floor(centerY + halfHeight) + guardTiles);
  }
  const unique = new Map<string, FineTileIndex>();

  for (let x = minX; x <= maxX; x += 1) {
    for (let y = minY; y <= maxY; y += 1) {
      const index = { z, x: canonicalX(x, scale), y };
      unique.set(`${z}/${index.x}/${y}`, index);
    }
  }
  return [...unique.values()];
}

/**
 * Atomically publishes a complete visible viewport, then expands it to a
 * complete rendered guard ring. Pending generations never leak partial data
 * into deck.gl, and obsolete guard work can never replace newer coverage.
 */
export class FineTileCoordinator {
  private readonly guardTiles: number;
  private readonly debounceMs: number;
  private readonly maxCachedTiles: number;
  private readonly onCommit: CoordinatorOptions['onCommit'];
  private readonly onLoadingChange: NonNullable<CoordinatorOptions['onLoadingChange']>;
  private readonly onError: NonNullable<CoordinatorOptions['onError']>;
  private readonly cache = new Map<string, { sourceKey: string; tile: CompactTile }>();
  private readonly inFlight = new Map<string, Promise<CompactTile>>();
  private committedSourceKey = '';
  private committedTileKeys = new Set<string>();
  private committedChunks: FineTileChunk[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;
  private generation = 0;
  private requestedSignature = '';
  private disposed = false;

  constructor(options: CoordinatorOptions) {
    this.guardTiles = options.guardTiles ?? 2;
    this.debounceMs = options.debounceMs ?? 100;
    this.maxCachedTiles = options.maxCachedTiles ?? 256;
    this.onCommit = options.onCommit;
    this.onLoadingChange = options.onLoadingChange ?? (() => {});
    this.onError = options.onError ?? (() => {});
  }

  schedule(camera: FineTileCamera, source: FineTileSource): void {
    if (this.disposed) return;
    const visibleIndices = fineTileIndices(
      camera,
      source.minZoom,
      source.maxZoom,
      0
    );
    const guardedIndices = fineTileIndices(
      camera,
      source.minZoom,
      source.maxZoom,
      this.guardTiles
    );
    const signature = `${source.key}:${visibleIndices
      .map(({ z, x, y }) => `${z}/${x}/${y}`).join(',')}`;
    if (signature === this.requestedSignature) return;
    this.requestedSignature = signature;
    // Every distinct camera signature supersedes all older visible and guard
    // work. Previously the covered-viewport fast path reused a generation,
    // allowing a slower obsolete guard to publish after the current camera.
    const generation = ++this.generation;
    if (this.timer) clearTimeout(this.timer);
    if (source.key === this.committedSourceKey && visibleIndices.every((index) =>
      this.committedTileKeys.has(this.tileKey(index)))) {
      this.onLoadingChange(false);
      if (guardedIndices.every((index) => this.committedTileKeys.has(this.tileKey(index)))) {
        return;
      }
      void this.publishGuard(generation, source, guardedIndices);
      return;
    }
    if (visibleIndices.every((index) => this.cache.has(this.cacheKey(source.key, index)))) {
      this.timer = null;
      this.commit(source.key, visibleIndices);
      this.onLoadingChange(false);
      void this.publishGuard(generation, source, guardedIndices);
      return;
    }
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.loadGeneration(generation, source, visibleIndices, guardedIndices);
    }, this.debounceMs);
  }

  reset(): void {
    this.generation += 1;
    this.requestedSignature = '';
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.onLoadingChange(false);
  }

  dispose(): void {
    this.disposed = true;
    this.reset();
    this.cache.clear();
    this.inFlight.clear();
    this.committedTileKeys.clear();
    this.committedChunks = [];
  }

  hasCoverage(camera: FineTileCamera, source: FineTileSource): boolean {
    if (source.key !== this.committedSourceKey) return false;
    return fineTileIndices(camera, source.minZoom, source.maxZoom, 0)
      .every((index) => this.committedTileKeys.has(this.tileKey(index)));
  }

  private async loadGeneration(
    generation: number,
    source: FineTileSource,
    visibleIndices: FineTileIndex[],
    guardedIndices: FineTileIndex[]
  ): Promise<void> {
    this.onLoadingChange(true);
    try {
      await Promise.all(visibleIndices.map((index) => this.loadTile(source, index)));
      if (this.disposed || generation !== this.generation) return;
      this.commit(source.key, visibleIndices);
      await this.loadAndCommitGuard(generation, source, guardedIndices);
    } catch (reason) {
      if (generation === this.generation) this.onError(reason);
    } finally {
      if (generation === this.generation) this.onLoadingChange(false);
    }
  }

  private async publishGuard(
    generation: number,
    source: FineTileSource,
    indices: FineTileIndex[]
  ): Promise<void> {
    this.onLoadingChange(true);
    try {
      await this.loadAndCommitGuard(generation, source, indices);
    } catch (reason) {
      if (!this.disposed && generation === this.generation) this.onError(reason);
    } finally {
      if (generation === this.generation) this.onLoadingChange(false);
    }
  }

  private async loadAndCommitGuard(
    generation: number,
    source: FineTileSource,
    indices: FineTileIndex[]
  ): Promise<void> {
    await Promise.all(indices.map((index) => this.loadTile(source, index)));
    if (this.disposed || generation !== this.generation) return;
    this.commit(source.key, indices);
  }

  private commit(sourceKey: string, indices: FineTileIndex[]): void {
    const tileKeys = new Set(indices.map((index) => this.tileKey(index)));
    if (sourceKey === this.committedSourceKey && tileKeys.size === this.committedTileKeys.size &&
      [...tileKeys].every((key) => this.committedTileKeys.has(key))) return;
    const chunks = this.chunksForTiles(sourceKey, tileKeys);
    this.committedSourceKey = sourceKey;
    this.committedTileKeys = tileKeys;
    this.committedChunks = chunks;
    this.onCommit({
      chunks,
      cellCount: chunks.reduce((total, chunk) => total + chunk.cells.length, 0),
      sourceKey,
      tileCount: indices.length,
      tileKeys
    });
  }

  private loadTile(source: FineTileSource, index: FineTileIndex): Promise<CompactTile> {
    const key = this.cacheKey(source.key, index);
    const cached = this.cache.get(key);
    if (cached) {
      this.cache.delete(key);
      this.cache.set(key, cached);
      return Promise.resolve(cached.tile);
    }
    const existing = this.inFlight.get(key);
    if (existing) return existing;
    const request = source.load(index).then((tile) => {
      this.cache.set(key, { sourceKey: source.key, tile });
      while (this.cache.size > this.maxCachedTiles) {
        const oldest = this.cache.keys().next().value;
        if (oldest === undefined) break;
        this.cache.delete(oldest);
      }
      return tile;
    }).finally(() => this.inFlight.delete(key));
    this.inFlight.set(key, request);
    return request;
  }

  private tileKey(index: FineTileIndex): string {
    return `${index.z}/${index.x}/${index.y}`;
  }

  private cacheKey(sourceKey: string, index: FineTileIndex): string {
    return `${sourceKey}:${this.tileKey(index)}`;
  }

  private chunksForTiles(sourceKey: string, tileKeys: ReadonlySet<string>): FineTileChunk[] {
    if (sourceKey === this.committedSourceKey && tileKeys.size === this.committedTileKeys.size &&
      [...tileKeys].every((key) => this.committedTileKeys.has(key))) return this.committedChunks;
    const chunks: FineTileChunk[] = [];
    for (const [cacheKey, entry] of this.cache) {
      if (entry.sourceKey !== sourceKey) continue;
      const tileKey = cacheKey.slice(sourceKey.length + 1);
      if (!tileKeys.has(tileKey)) continue;
      chunks.push({ key: tileKey, cells: entry.tile.cells });
    }
    return chunks;
  }
}
