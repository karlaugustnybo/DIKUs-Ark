<script lang="ts">
  import { Dna, MapPinned, RotateCcw, Search, ShieldAlert, SlidersHorizontal, Trees, X } from 'lucide-svelte';
  import BoundaryFilter from './BoundaryFilter.svelte';
  import { boundaryFilters, clearBoundaryFilters } from '$lib/stores/boundaries';
  import { tableDna, tableRedlist, tableSearch, tableSystems } from '$lib/stores/species';

  const redlistOptions = [
    ['Critically Endangered', 'CR', 'cr'], ['Endangered', 'EN', 'en'], ['Vulnerable', 'VU', 'vu'],
    ['Near Threatened', 'NT', 'nt'], ['Data Deficient', 'DD', 'dd'], ['Least Concern', 'LC', 'lc'],
    ['Not Assessed', 'NA', 'na']
  ];
  const dnaOptions = [
    ['goat_data_deficient', 'Data deficient', 'data-deficient'],
    ['missing_family', 'Missing family', 'family'], ['missing_genus', 'Missing genus', 'genus'],
    ['missing_species', 'Missing species', 'species'], ['sampled', 'Sampled', 'sampled']
  ];
  const systemOptions = ['Terrestrial', 'Freshwater', 'Marine'];

  $: spatialCount = Object.values($boundaryFilters).reduce((sum, values) => sum + values.length, 0);
  $: filterCount = $tableRedlist.length + $tableDna.length + $tableSystems.length + spatialCount;

  function toggle(store: typeof tableRedlist, value: string) {
    store.update((selected) => selected.includes(value)
      ? selected.filter((item) => item !== value) : [...selected, value]);
  }

  function clear() {
    tableSearch.set(''); tableRedlist.set([]); tableDna.set([]); tableSystems.set([]);
    clearBoundaryFilters();
  }
</script>

<section class="table-filter-deck" aria-label="Species table filters">
  <div class="filter-deck-heading">
    <div class="filter-heading-copy"><SlidersHorizontal size={17} strokeWidth={1.7} aria-hidden="true" /><strong>Species filters</strong></div>
    {#if filterCount || $tableSearch}<button type="button" class="clear-filters" on:click={clear}><RotateCcw size={13} strokeWidth={1.8} />Reset all</button>{/if}
  </div>
  <label class="table-search-field">
    <Search size={18} strokeWidth={1.65} aria-hidden="true" />
    <span class="sr-only">Search species and families</span>
    <input aria-label="Search species and families" type="search" maxlength="120" placeholder="Search species or family…" bind:value={$tableSearch} />
    {#if $tableSearch}<button type="button" aria-label="Clear search" on:click={() => tableSearch.set('')}><X size={16} strokeWidth={1.8} /></button>{/if}
  </label>

  <div class="facet-grid">
    <fieldset><legend><ShieldAlert size={14} strokeWidth={1.7} />IUCN status</legend><div class="facet-pills">{#each redlistOptions as option}<button type="button" class={`tone-${option[2]}`} class:active={$tableRedlist.includes(option[0])} aria-pressed={$tableRedlist.includes(option[0])} on:click={() => toggle(tableRedlist, option[0])} title={option[0]}>{option[1]}</button>{/each}</div></fieldset>
    <fieldset><legend><Dna size={14} strokeWidth={1.7} />DNA representation</legend><div class="facet-pills is-wide">{#each dnaOptions as option}<button type="button" class={`tone-${option[2]}`} class:active={$tableDna.includes(option[0])} aria-pressed={$tableDna.includes(option[0])} on:click={() => toggle(tableDna, option[0])}>{option[1]}</button>{/each}</div></fieldset>
    <fieldset><legend><Trees size={14} strokeWidth={1.7} />Ecosystem</legend><div class="facet-pills is-wide ecosystem-pills">{#each systemOptions as option}<button type="button" class:active={$tableSystems.includes(option)} aria-pressed={$tableSystems.includes(option)} on:click={() => toggle(tableSystems, option)}>{option}</button>{/each}</div></fieldset>
  </div>

  <div class="spatial-row">
    <div><span><MapPinned size={14} strokeWidth={1.7} />Spatial occurrence</span><small>Only species recorded in H3 cells that touch every selected boundary framework.</small></div>
    <BoundaryFilter condensed showSummary={false} />
  </div>
  <footer><span class:active={filterCount > 0} class="filter-status-dot"></span><span>{filterCount ? `${filterCount} active filter${filterCount === 1 ? '' : 's'}` : 'Showing the complete dataset'}</span></footer>
</section>

<style>
  .table-filter-deck { width: min(90%, 1180px); margin: var(--space-24) auto; border: 1px solid var(--color-tinted-cream); border-radius: var(--border-radius-lg); background: var(--color-light-cream); box-shadow: var(--box-shadow-sm); text-align: left; font-family: var(--font-sans); }
  .filter-deck-heading { display: flex; justify-content: space-between; align-items: center; padding: 1rem 1.2rem .75rem; }
  .filter-heading-copy { display: flex; align-items: center; gap: .55rem; color: var(--color-tertiary-green); }
  .filter-heading-copy strong { color: var(--color-gray-800); font-size: 1rem; line-height: 1; }
  .clear-filters { display: inline-flex; align-items: center; gap: .28rem; padding: .3rem; color: var(--color-accent-brown); font-size: .75rem; font-weight: 650; }
  .table-search-field { display: flex; align-items: center; gap: .65rem; margin: 0 1.2rem 1rem; padding: .68rem .8rem; border: 1px solid var(--color-gray-300); border-radius: var(--border-radius-base); background: var(--color-cream); color: var(--color-gray-500); }
  .table-search-field:focus-within { border-color: var(--color-primary-green); box-shadow: 0 0 0 3px var(--color-primary-green-10); }
  .table-search-field input { flex: 1; border: 0; outline: 0; background: transparent; color: var(--color-black); font-size: .88rem; }
  .table-search-field button { display: grid; place-items: center; padding: 0 .25rem; color: var(--color-gray-500); }
  .facet-grid { display: grid; grid-template-columns: 1.1fr 1.4fr 1fr; gap: 1rem; padding: .15rem 1.2rem 1rem; }
  fieldset { min-width: 0; margin: 0; padding: 0; border: 0; }
  legend { display: inline-flex; align-items: center; gap: .35rem; padding: 0; color: var(--color-gray-700); font-size: .7rem; font-weight: 700; letter-spacing: .025em; }
  legend :global(svg) { color: var(--color-tertiary-green); }
  .facet-pills { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; margin-top: .5rem; }
  .facet-pills button { display: inline-flex; align-items: center; justify-content: center; min-width: 2.35rem; min-height: 1.8rem; padding: .38rem .58rem; border: 1px solid transparent; border-radius: 999px; font-size: .67rem; font-weight: 650; line-height: 1; opacity: .68; transition: opacity 120ms ease, border-color 120ms ease, box-shadow 120ms ease, transform 120ms ease; }
  .facet-pills button:hover { opacity: 1; transform: translateY(-1px); }
  .facet-pills button.active { border-color: currentColor; opacity: 1; }
  .facet-pills button:focus-visible { outline: 2px solid var(--color-primary-green); outline-offset: 2px; }
  .tone-cr { background: #f2d5d3; color: #81291f; }
  .tone-family { background: #f4d9d5; color: #81291f; }
  .tone-en { background: #f5ddca; color: #87401f; }
  .tone-vu { background: #f4e6bd; color: #6d520b; }
  .tone-nt { background: #e3e5c8; color: #4f5a1d; }
  .tone-lc, .tone-sampled { background: #dcebd4; color: #315d2b; }
  .tone-dd, .tone-na { background: var(--color-gray-200); color: var(--color-gray-700); }
  .tone-data-deficient { background: #e4e0ea; color: #594b6b; }
  .tone-genus { background: #f7e3c2; color: #794b09; }
  .tone-species { background: #eee9c9; color: #5c5310; }
  .ecosystem-pills button { border-color: #c9d8ce; background: #e4eee7; color: #315d47; }
  .facet-pills.is-wide button { min-width: 0; }
  .spatial-row { padding: .2rem 1.2rem .9rem; }
  .spatial-row > div:first-child { margin-bottom: .7rem; }
  .spatial-row > div:first-child span { display: flex; align-items: center; gap: .35rem; color: var(--color-gray-800); font-size: .78rem; font-weight: 700; }
  .spatial-row > div:first-child span :global(svg) { color: var(--color-tertiary-green); }
  .spatial-row small { display: block; color: var(--color-gray-500); font-family: var(--font-body); font-size: .7rem; }
  footer { display: flex; align-items: center; gap: .4rem; padding: .55rem 1.2rem; border-top: 1px solid var(--color-gray-200); color: var(--color-gray-500); font-size: .68rem; }
  .filter-status-dot { width: .35rem; height: .35rem; border-radius: 50%; background: var(--color-gray-400); }
  .filter-status-dot.active { background: var(--color-primary-green); box-shadow: 0 0 0 3px var(--color-primary-green-10); }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
  @media (max-width: 850px) { .facet-grid { grid-template-columns: 1fr; gap: .8rem; } }
</style>
