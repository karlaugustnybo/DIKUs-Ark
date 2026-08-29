<script lang="ts">
  import {
    colourPalette, habitatSystem, normalizeColoursBySpecies, type HabitatSystem
  } from '$lib/stores/map';
  import { colourPalettes, type ColourPaletteId } from '$lib/map/colourPalettes';
  import WeightControls from './WeightControls.svelte';
  import BoundaryFilter from './BoundaryFilter.svelte';

  export let showSystems = true;
  let colourMenuOpen = false;
  let colourControl: HTMLDivElement;
  $: activePalette = colourPalettes.find((palette) => palette.id === $colourPalette) ?? colourPalettes[0];
  $: activeGradientStops = activePalette.cssGradient.match(/#[0-9a-f]{6}/gi) ?? [];

  function closeColourMenu() {
    colourMenuOpen = false;
  }

  function handleWindowClick(event: MouseEvent) {
    if (colourMenuOpen && !colourControl?.contains(event.target as Node)) closeColourMenu();
  }

  function handleWindowKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && colourMenuOpen) closeColourMenu();
  }

  const systems: Array<{ value: HabitatSystem; label: string }> = [
    { value: '', label: 'All' }, { value: 'Terrestrial', label: 'Terrestrial' },
    { value: 'Freshwater', label: 'Freshwater' }, { value: 'Marine', label: 'Marine' }
  ];
  const iucn = [
    { key: 'cr' as const, label: 'Critically Endangered' }, { key: 'en' as const, label: 'Endangered' },
    { key: 'vu' as const, label: 'Vulnerable' }, { key: 'nt' as const, label: 'Near Threatened' },
    { key: 'dd' as const, label: 'Data Deficient' }, { key: 'lc' as const, label: 'Least Concern', step: 0.1 }
  ];
  const dna = [
    { key: 'gdd' as const, label: 'GoaT Data Deficient' },
    { key: 'sp' as const, label: 'Missing Species DNA' }, { key: 'gen' as const, label: 'Missing Genus DNA' },
    { key: 'fam' as const, label: 'Missing Family DNA' },
    { key: 'samp' as const, label: 'Already Sampled', step: 0.1 }
  ];
</script>

<svelte:window on:click={handleWindowClick} on:keydown={handleWindowKeydown} />

<div class="sliders-wrapper">
  {#if showSystems}
    <div class="map-options">
      <div class="system-filter no-margin-top">
        <div class="pill-tabs" aria-label="Ecosystem filter">
          <span class="pill-indicator" aria-hidden="true" style:left={`${4 + systems.findIndex((item) => item.value === $habitatSystem) * 114}px`}></span>
          {#each systems as item}
            <button type="button" class:is-active={$habitatSystem === item.value} class="pill-tab" on:click={() => habitatSystem.set(item.value)}>{item.label}</button>
          {/each}
        </div>
      </div>
      <div class="normalization-control" class:is-open={colourMenuOpen} bind:this={colourControl}>
        <button
          type="button"
          class="normalization-button"
          class:is-active={colourMenuOpen}
          aria-label="Map colour and normalization options"
          aria-expanded={colourMenuOpen}
          aria-controls="map-colour-menu"
          aria-describedby="normalization-tooltip"
          on:click={() => colourMenuOpen = !colourMenuOpen}
        >
          <svg class="gradient-bucket" width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true" shape-rendering="geometricPrecision">
            <defs>
              <linearGradient id={`bucket-gradient-${activePalette.id}`} x1="2" y1="12" x2="22" y2="12" gradientUnits="userSpaceOnUse">
                {#each activeGradientStops as colour, index}
                  <stop offset={`${index / Math.max(1, activeGradientStops.length - 1) * 100}%`} stop-color={colour} />
                {/each}
              </linearGradient>
            </defs>
            <g stroke={`url(#bucket-gradient-${activePalette.id})`} stroke-width="2.15" stroke-linecap="round" stroke-linejoin="round">
              <path d="M11 7 6 2" />
              <path d="M18.992 12H2.041" />
              <path d="M21.145 18.38A3.34 3.34 0 0 1 20 16.5a3.3 3.3 0 0 1-1.145 1.88c-.575.46-.855 1.02-.855 1.595A2 2 0 0 0 20 22a2 2 0 0 0 2-2.025c0-.58-.285-1.13-.855-1.595" />
              <path d="m8.5 4.5 2.148-2.148a1.205 1.205 0 0 1 1.704 0l7.296 7.296a1.205 1.205 0 0 1 0 1.704l-7.592 7.592a3.615 3.615 0 0 1-5.112 0l-3.888-3.888a3.615 3.615 0 0 1 0-5.112L5.67 7.33" />
            </g>
          </svg>
        </button>
        <span id="normalization-tooltip" class="normalization-tooltip" role="tooltip">Map colour options</span>
        {#if colourMenuOpen}
          <div id="map-colour-menu" class="colour-menu" role="dialog" aria-label="Map colour options">
            <div class="colour-menu-heading">
              <strong>Map colours</strong>
              <span>Low → high priority</span>
            </div>
            <div class="palette-options" role="radiogroup" aria-label="Colour palette">
              {#each colourPalettes as palette}
                <button
                  type="button"
                  class="palette-option"
                  class:is-selected={$colourPalette === palette.id}
                  role="radio"
                  aria-checked={$colourPalette === palette.id}
                  on:click={() => colourPalette.set(palette.id as ColourPaletteId)}
                >
                  <span class="palette-option-swatch" style:--palette-gradient={palette.cssGradient} aria-hidden="true"></span>
                  <span>{palette.label.replace(' · accessible', '')}</span>
                  <span class="palette-selected-mark" aria-hidden="true"></span>
                </button>
              {/each}
            </div>
            <button
              type="button"
              class="normalization-option"
              role="switch"
              aria-checked={$normalizeColoursBySpecies}
              on:click={() => normalizeColoursBySpecies.update((enabled) => !enabled)}
            >
              <span>
                <strong>Per species</strong>
                <small>Normalize scores by species count</small>
              </span>
              <span class="menu-switch" class:is-active={$normalizeColoursBySpecies} aria-hidden="true"><i></i></span>
            </button>
          </div>
        {/if}
      </div>
    </div>
    <BoundaryFilter />
  {/if}
  <WeightControls title="IUCN Category Weights" controls={iucn} />
  <WeightControls title={showSystems ? 'DNA & Coverage Weights' : 'DNA Weights'} controls={dna} />
</div>
