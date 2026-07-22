<script lang="ts">
  import { onMount } from 'svelte';
  import { api, type StatsResponse } from '$lib/api/client';

  let stats: StatsResponse | null = null;
  let error = '';

  onMount(async () => {
    try { stats = await api.stats(); }
    catch (reason) { error = reason instanceof Error ? reason.message : 'Unable to load statistics'; }
  });
</script>

<svelte:head><title>Ark-IV</title></svelte:head>

<main class="main-content">
  <div class="homepage-wrapper">
    <header class="homepage-hero">
      <h1><span class="hero-name">Ark-<i>IV</i></span><span class="hero-intro"> helps prioritise where to sample DNA, directing efforts to preserve at-risk species before extinction takes them beyond our reach.</span></h1>
      <p class="hero-tagline">Extinction should not mean erasure.</p>
    </header>

    <div class="section-divider"><span>Explore</span></div>

    <div class="feature-grid">
      <a href="/table" class="feature-card">
        <div class="feature-icon"><svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3H5a2 2 0 0 0-2 2v4m6-6h10a2 2 0 0 1 2 2v4M9 3v18m0 0h10a2 2 0 0 0 2-2V9M9 21H5a2 2 0 0 1-2-2V9m0 0h18"/></svg></div>
        <h3>Species Table</h3><p>Browse, filter, and sort every species in the database. Adjust scoring weights to discover which animals need DNA sequencing the most.</p>
        <div class="feature-arrow"><svg class="arrow-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg><span>Explore the Table</span></div>
      </a>
      <a href="/map" class="feature-card">
        <div class="feature-icon"><svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.54 15H17a2 2 0 0 0-2 2v4.54"/><path d="M7 3.34V5a3 3 0 0 0 3 3a2 2 0 0 1 2 2c0 1.1.9 2 2 2a2 2 0 0 0 2-2c0-1.1.9-2 2-2h3.17"/><path d="M11 21.95V18a2 2 0 0 0-2-2a2 2 0 0 1-2-2v-1a2 2 0 0 0-2-2H2.05"/><circle cx="12" cy="12" r="10"/></svg></div>
        <h3>Interactive Heatmap</h3><p>Visualise Denmark conservation priorities on an interactive map. Zoom into any region and explore H3 hexagon aggregations in real time.</p>
        <div class="feature-arrow"><svg class="arrow-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg><span>Open the Map</span></div>
      </a>
      <a href="/tutorial" class="feature-card">
        <div class="feature-icon"><svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" x2="12.01" y1="17" y2="17"/></svg></div>
        <h3>Tutorial</h3><p>Learn how to navigate the table and map, understand the weighting system, and build your own custom conservation scores.</p>
        <div class="feature-arrow"><svg class="arrow-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg><span>Read the Tutorial</span></div>
      </a>
    </div>

    <div class="section-divider"><span>At a Glance</span></div>
    {#if error}<p class="error-message">{error}</p>{/if}
    <div class="stats-grid">
      {#each [
        [stats?.total, 'Species'], [stats?.critically_endangered, 'Critically Endangered'], [stats?.edge_species, 'EDGE Species'],
        [stats?.needs_dna_sampling, 'Needs DNA Sampling'], [stats?.res3_cells, 'Large H3 Cells'], [stats?.res7_cells, 'Small H3 Cells']
      ] as item}
        <div class="stat-item"><div class="stat-number">{item[0]?.toLocaleString() ?? '—'}</div><div class="stat-label">{item[1]}</div></div>
      {/each}
    </div>
  </div>
</main>
