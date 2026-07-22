<script lang="ts">
  import { habitatSystem, type HabitatSystem } from '$lib/stores/map';
  import WeightControls from './WeightControls.svelte';

  export let showSystems = true;
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
    { key: 'sp' as const, label: 'Missing Species DNA' }, { key: 'gen' as const, label: 'Missing Genus DNA' },
    { key: 'fam' as const, label: 'Missing Family DNA' },
    ...(showSystems ? [{ key: 'cov' as const, label: 'DNA Coverage Weight' }] : []),
    { key: 'samp' as const, label: 'Already Sampled', step: 0.1 }
  ];
</script>

<div class="sliders-wrapper">
  {#if showSystems}
    <div class="system-filter no-margin-top">
      <div class="pill-tabs" aria-label="Ecosystem filter">
        <span class="pill-indicator" aria-hidden="true" style:left={`${4 + systems.findIndex((item) => item.value === $habitatSystem) * 114}px`}></span>
        {#each systems as item}
          <button type="button" class:is-active={$habitatSystem === item.value} class="pill-tab" on:click={() => habitatSystem.set(item.value)}>{item.label}</button>
        {/each}
      </div>
    </div>
  {/if}
  <WeightControls title="IUCN Category Weights" controls={iucn} />
  <WeightControls title={showSystems ? 'DNA & Coverage Weights' : 'DNA Weights'} controls={dna} />
</div>
