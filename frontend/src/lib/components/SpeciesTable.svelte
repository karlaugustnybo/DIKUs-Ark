<script lang="ts">
  import { onMount } from 'svelte';
  import { get } from 'svelte/store';
  import { api, type SpeciesPage } from '$lib/api/client';
  import SpeciesDataTable from '$lib/components/SpeciesDataTable.svelte';
  import { dnaStatus, type SpeciesSortKey, type SpeciesTableRow } from '$lib/species-table';
  import { weights } from '$lib/stores/weights';
  import { boundaryFilters } from '$lib/stores/boundaries';
  import { tableDna, tableOrder, tablePage, tableRedlist, tableSearch, tableSort, tableSystems } from '$lib/stores/species';

  let result: SpeciesPage | null = null;
  let loading = true;
  let error = '';
  let debounce: ReturnType<typeof setTimeout>;
  let perPage = 10;
  let requestId = 0;
  let abortController: AbortController | null = null;

  $: tableRows = (result?.rows ?? []).map((row): SpeciesTableRow => ({
    ...row,
    dnaLabel: row.dna_level,
    dnaStatus: dnaStatus(row.dna_level)
  }));

  function params() {
    const current = get(weights);
    const filters = get(boundaryFilters);
    return new URLSearchParams({
      search: get(tableSearch), sort: get(tableSort), order: get(tableOrder),
      page: String(get(tablePage)), per_page: String(perPage),
      redlist: get(tableRedlist).join(','), dna: get(tableDna).join(','),
      systems: get(tableSystems).join(','),
      admin0: (filters.admin0 ?? []).join(','), admin1: (filters.admin1 ?? []).join(','),
      municipality: (filters.municipality ?? []).join(','),
      eez: (filters.eez ?? []).join(','),
      conservation_framework: (filters.conservation_framework ?? []).join(','),
      ...Object.fromEntries(Object.entries(current).map(([key, value]) => [key, String(value)]))
    });
  }

  async function load() {
    const currentRequest = ++requestId;
    abortController?.abort();
    const controller = new AbortController();
    abortController = controller;
    loading = true;
    error = '';
    try {
      const nextResult = await api.species(params(), controller.signal);
      if (currentRequest === requestId) result = nextResult;
    }
    catch (reason) {
      if (currentRequest === requestId && !(reason instanceof DOMException && reason.name === 'AbortError')) {
        error = reason instanceof Error ? reason.message : 'Unable to load species';
      }
    }
    finally {
      if (currentRequest === requestId) loading = false;
      if (abortController === controller) abortController = null;
    }
  }

  function schedule(resetPage = false) {
    if (resetPage) tablePage.set(1);
    clearTimeout(debounce);
    abortController?.abort();
    requestId += 1;
    loading = true;
    error = '';
    debounce = setTimeout(load, 250);
  }

  function sort(key: SpeciesSortKey) {
    if (get(tableSort) === key) tableOrder.update((value) => value === 'asc' ? 'desc' : 'asc');
    else {
      tableSort.set(key);
      tableOrder.set(key === 'species_name' || key === 'family' || key === 'redlist_category' ? 'asc' : 'desc');
    }
    schedule(true);
  }

  function page(delta: number) {
    if (!result || loading) return;
    const nextPage = Math.min(result.total_pages, Math.max(1, result.page + delta));
    if (nextPage === result.page) return;
    tablePage.set(nextPage);
    load();
  }

  function keydown(event: KeyboardEvent) {
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || !result) return;
    if (event.key === 'ArrowLeft' && result.page > 1) {
      event.preventDefault();
      page(-1);
    }
    else if (event.key === 'ArrowRight' && result.page < result.total_pages) {
      event.preventDefault();
      page(1);
    }
    else {
      const shortcuts: Partial<Record<string, SpeciesSortKey>> = {
        '1': 'species_name', '2': 'family', '3': 'redlist_category', '4': 'dna_level', '5': 'priority'
      };
      const key = shortcuts[event.key];
      if (key) sort(key);
    }
  }

  const subscriptions = [weights, tableSearch, tableRedlist, tableDna, tableSystems, boundaryFilters]
    .map((store) => store.subscribe(() => { if (result) schedule(true); }));
  onMount(() => {
    load();
    return () => {
      subscriptions.forEach((unsubscribe) => unsubscribe());
      clearTimeout(debounce);
      requestId += 1;
      abortController?.abort();
    };
  });
</script>

<svelte:window on:keydown={keydown} />

<div class="species-table-toolbar table-results-toolbar">
  <span>{result ? `${result.total.toLocaleString()} matching species` : 'Species results'}</span>
  <label class="page-size">Rows <select bind:value={perPage} on:change={() => schedule(true)}><option value={10}>10</option><option value={25}>25</option><option value={50}>50</option></select></label>
</div>
{#if error}<p class="error-message">{error}</p>{/if}
{#if result?.suggested && $tableSearch.trim()}
  <p class="search-notice" role="status">No exact matches for “{$tableSearch.trim()}”. Showing similar names.</p>
{/if}
<SpeciesDataTable
  rows={tableRows}
  sort={$tableSort}
  order={$tableOrder}
  {loading}
  onSort={sort}
  emptyMessage={$tableSearch.trim()
    ? `No species or families match “${$tableSearch.trim()}”. Try a shorter name or check the spelling.`
    : 'No species match these filters.'}
/>

{#if result}
  <div class="pagination">
    <span class="page-info"><strong>{result.total.toLocaleString()}</strong> species · Page {result.page} of {result.total_pages}</span>
    <div class="pagination-buttons">
      <button class="btn-secondary" disabled={loading || result.page <= 1} on:click={() => page(-1)}>Previous</button>
      <button class="btn-secondary" disabled={loading || result.page >= result.total_pages} on:click={() => page(1)}>Next</button>
    </div>
  </div>
{/if}
