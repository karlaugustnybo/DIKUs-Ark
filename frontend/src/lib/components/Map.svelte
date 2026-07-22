<script lang="ts">
  import { onMount } from 'svelte';
  import { get } from 'svelte/store';
  import { Deck, MapView } from '@deck.gl/core';
  import { MVTLayer, TileLayer } from '@deck.gl/geo-layers';
  import { BitmapLayer } from '@deck.gl/layers';
  import { CompassWidget, FullscreenWidget, ZoomWidget } from '@deck.gl/widgets';
  import { PMTiles } from 'pmtiles';
  import { config } from '$lib/config';
  import { habitatSystem, resolution, selectedH3 } from '$lib/stores/map';
  import { weights } from '$lib/stores/weights';

  type TileProperties = {
    h3_index: string; resolution: number; system: string; total: number;
    cr: number; en: number; vu: number; nt: number; dd: number; lc: number;
    ms: number; mg: number; mf: number;
  };
  type Feature = { properties: TileProperties };
  type ScoreDomain = { min: number; max: number };
  type MapMetadata = { score_domains: Record<string, ScoreDomain> };

  const RESOLUTION_SWITCH_ZOOM = 7;
  const TILE_CACHE_SIZE = 256;

  let container: HTMLDivElement;
  let deck: Deck<any> | null = null;
  let archive: PMTiles;
  let currentResolution: 3 | 7 = 3;
  let currentWeights = get(weights);
  let currentSystem = get(habitatSystem);
  let currentSelected = get(selectedH3);
  let scoreDomains: Record<string, ScoreDomain> = {};
  let domainMin = 0;
  let domainMax = 1;
  let mapError = '';
  let subscriptions: Array<() => void> = [];
  const tileCache = new Map<string, Promise<Uint8Array>>();

  function score(properties: TileProperties): number {
    return properties.cr * currentWeights.cr + properties.en * currentWeights.en + properties.vu * currentWeights.vu +
      properties.nt * currentWeights.nt + properties.dd * currentWeights.dd + properties.lc * currentWeights.lc +
      properties.ms * currentWeights.sp + properties.mg * currentWeights.gen + properties.mf * currentWeights.fam;
  }

  // Matches the Turbo colour map used by the original Flask implementation.
  function turbo(value: number): [number, number, number, number] {
    const x = Math.min(1, Math.max(0, value));
    const red = 0.13572138 + x * (4.61539260 + x * (-42.66032258 + x * (132.13108234 + x * (-152.94239396 + x * 59.28637943))));
    const green = 0.09140261 + x * (2.19418839 + x * (4.84296658 + x * (-14.18503333 + x * (4.27729857 + x * 2.82956604))));
    const blue = 0.10667330 + x * (12.64194608 + x * (-60.58204836 + x * (110.36276771 + x * (-89.90310912 + x * 27.34824973))));
    const channel = (value: number) => Math.round(Math.min(1, Math.max(0, value)) * 255);
    return [channel(red), channel(green), channel(blue), 50];
  }
  const turboColors = Array.from({ length: 256 }, (_, index) => turbo(index / 255));

  function fillColor(properties: TileProperties): [number, number, number, number] {
    const value = score(properties);
    const normalized = domainMax > domainMin ? (value - domainMin) / (domainMax - domainMin) : value > 0 ? 1 : 0;
    return turboColors[Math.round(Math.min(1, Math.max(0, normalized)) * 255)];
  }

  function useSystemDomain(system: string) {
    const key = system.toLowerCase() || 'all';
    const domain = scoreDomains[key];
    if (!domain || !Number.isFinite(domain.min) || !Number.isFinite(domain.max) || domain.max <= domain.min) {
      throw new Error(`Map metadata contains an invalid score domain for ${key}`);
    }
    domainMin = domain.min;
    domainMax = domain.max;
  }

  function tileBytes(z: number, x: number, y: number): Promise<Uint8Array> {
    const key = `${z}/${x}/${y}`;
    const cached = tileCache.get(key);
    if (cached) {
      tileCache.delete(key);
      tileCache.set(key, cached);
      return cached;
    }
    const pending = archive.getZxy(z, x, y)
      .then((tile) => tile ? new Uint8Array(tile.data) : new Uint8Array())
      .catch((error) => {
        tileCache.delete(key);
        throw error;
      });
    tileCache.set(key, pending);
    if (tileCache.size > TILE_CACHE_SIZE) {
      const oldest = tileCache.keys().next().value;
      if (oldest !== undefined) tileCache.delete(oldest);
    }
    return pending;
  }

  async function pmtilesFetch(url: string, options?: RequestInit): Promise<Response> {
    const match = url.match(/\/(\d+)\/(\d+)\/(\d+)\.mvt$/);
    if (!match) return fetch(url, options);
    const data = await tileBytes(Number(match[1]), Number(match[2]), Number(match[3]));
    if (options?.signal?.aborted) throw new DOMException('Tile request aborted', 'AbortError');
    const body = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength) as ArrayBuffer;
    return new Response(body, { status: 200, headers: { 'Content-Type': 'application/vnd.mapbox-vector-tile' } });
  }

  function priorityLayer(cellResolution: 3 | 7, suffix: string, selected: string | null) {
    const layerName = `res${cellResolution}_${suffix}`;
    return new MVTLayer<TileProperties>({
      id: `priorities-${layerName}`,
      data: 'pmtiles://ark-iv/{z}/{x}/{y}.mvt',
      minZoom: cellResolution === 3 ? 0 : 6,
      maxZoom: cellResolution === 3 ? 7 : 12,
      visibleMaxZoom: cellResolution === 3 ? RESOLUTION_SWITCH_ZOOM : null,
      visibleMinZoom: cellResolution === 7 ? RESOLUTION_SWITCH_ZOOM + Number.EPSILON : null,
      maxCacheSize: 200,
      maxRequests: 12,
      // The polygons are translucent, so fallback parent and child tiles must
      // not overlap while a new zoom level is loading.
      refinementStrategy: 'no-overlap', binary: true,
      pickable: true, autoHighlight: true,
      highlightColor: [255, 255, 255, 50],
      uniqueIdProperty: 'h3_index', highlightedFeatureId: selected,
      loadOptions: { fetch: pmtilesFetch as any, mvt: { layers: [layerName] } },
      filled: true, stroked: false, extruded: true,
      getElevation: cellResolution === 3 ? 2000 : 500,
      getFillColor: (feature: Feature) => fillColor(feature.properties),
      updateTriggers: { getFillColor: [domainMin, domainMax, JSON.stringify(currentWeights)] },
      onClick: ({ object }: any) => {
        if (object?.properties?.h3_index) selectedH3.set(object.properties.h3_index);
      }
    });
  }

  function layers() {
    const suffix = currentSystem.toLowerCase() || 'all';
    const basemap = new TileLayer({
      id: 'carto-light', data: config.basemapUrl, refinementStrategy: 'best-available',
      maxCacheSize: 200, minZoom: 0, maxZoom: 19, tileSize: 256,
      renderSubLayers: (props: any) => {
        const { west, south, east, north } = props.tile.bbox;
        return new BitmapLayer(props, { data: null as any, image: props.data, bounds: [west, south, east, north] });
      }
    });
    return [basemap, priorityLayer(3, suffix, currentSelected), priorityLayer(7, suffix, currentSelected)];
  }

  function refresh() {
    if (deck) deck.setProps({ layers: layers() });
  }

  function keydown(event: KeyboardEvent) {
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
    if (event.key.toLowerCase() === 'f') {
      if (document.fullscreenElement) document.exitFullscreen();
      else container.parentElement?.requestFullscreen();
      return;
    }
    const systems = { '1': '', '2': 'Terrestrial', '3': 'Freshwater', '4': 'Marine' } as const;
    const system = systems[event.key as keyof typeof systems];
    if (system !== undefined) habitatSystem.set(system);
  }

  onMount(() => {
    let disposed = false;
    async function initialize() {
      const response = await fetch(config.mapMetadataUrl, { cache: 'no-cache' });
      if (!response.ok) throw new Error(`Unable to load map metadata (${response.status})`);
      const metadata = await response.json() as MapMetadata;
      if (!metadata.score_domains) throw new Error('Map metadata is out of date; rebuild the map artifacts');
      scoreDomains = metadata.score_domains;
      useSystemDomain(currentSystem);
      if (disposed) return;

      archive = new PMTiles(config.tileUrl);
      deck = new Deck({
      parent: container,
      views: new MapView({ repeat: true }),
      controller: {
        doubleClickZoom: true, dragPan: true, dragRotate: true,
        scrollZoom: true, touchRotate: true,
        keyboard: { moveSpeed: 100, rotateSpeedX: 15, rotateSpeedY: 15 }
      },
      initialViewState: { longitude: 10, latitude: 56, zoom: 4, minZoom: 0, maxZoom: 19 },
      widgets: [new FullscreenWidget({ placement: 'top-right' }), new ZoomWidget({ placement: 'top-right' }), new CompassWidget({ placement: 'top-right' })],
      layers: layers(),
      onViewStateChange: ({ viewState }) => {
        const next = viewState.zoom > RESOLUTION_SWITCH_ZOOM ? 7 : 3;
        if (next !== currentResolution) {
          currentResolution = next;
          resolution.set(next);
        }
      },
      getTooltip: ({ object }: any) => object ? {
        html: `<strong style="color:var(--color-black);">H3: ${object.properties.h3_index}</strong><br>` +
          `<span style="color:var(--color-gray-600);">Score: ${score(object.properties).toFixed(2)}</span><br>` +
          `<hr style="border-color:rgba(100,100,100,.2);margin:.4rem 0;">` +
          `<span style="color:var(--color-gray-600);font-size:.85em;">CR: ${object.properties.cr}, EN: ${object.properties.en}, VU: ${object.properties.vu}<br>` +
          `NT: ${object.properties.nt}, DD: ${object.properties.dd}, LC: ${object.properties.lc}<br>` +
          `Missing Species DNA: ${object.properties.ms}<br>Missing Genus DNA: ${object.properties.mg}<br>Missing Family DNA: ${object.properties.mf}</span>`,
        style: {
          minWidth: '200px', fontFamily: 'var(--font-sans)', backgroundColor: 'rgba(252,251,247,.97)',
          color: 'var(--color-black)', padding: '.75rem 1rem', borderRadius: 'var(--border-radius-base)',
          fontSize: '.9rem', lineHeight: '1.4rem', boxShadow: 'var(--box-shadow-sm)',
          border: '1px solid var(--color-tinted-cream)'
        }
      } : null
      });

      let subscriptionsReady = false;
      subscriptions = [
        weights.subscribe((value) => {
          currentWeights = value;
          if (subscriptionsReady) refresh();
        }),
        habitatSystem.subscribe((system) => {
          currentSystem = system;
          useSystemDomain(system);
          if (subscriptionsReady) refresh();
        }),
        selectedH3.subscribe((selected) => {
          currentSelected = selected;
          if (subscriptionsReady) refresh();
        })
      ];
      subscriptionsReady = true;
    }

    void initialize().catch((reason) => {
      console.error(reason);
      mapError = reason instanceof Error ? reason.message : 'Unable to initialize the map';
    });
    return () => {
      disposed = true;
      subscriptions.forEach((unsubscribe) => unsubscribe());
      tileCache.clear();
      deck?.finalize();
      deck = null;
    };
  });
</script>

<svelte:window on:keydown={keydown} />

<div id="map" class="map-shell">
  <div class="map-canvas" bind:this={container} aria-label="Interactive H3 conservation priority map"></div>
  {#if mapError}<p class="error-message">{mapError}</p>{/if}
</div>
