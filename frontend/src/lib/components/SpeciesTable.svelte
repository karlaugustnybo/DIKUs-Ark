<script lang="ts">
  import { onMount } from 'svelte';
  import { get } from 'svelte/store';
  import { api, type SpeciesPage } from '$lib/api/client';
  import { weights } from '$lib/stores/weights';
  import { tableOrder, tablePage, tableSearch, tableSort, type SortKey } from '$lib/stores/species';

  let result: SpeciesPage | null = null;
  let loading = true;
  let error = '';
  let debounce: ReturnType<typeof setTimeout>;

  const columns: Array<{ key: SortKey; label: string }> = [
    { key: 'species_name', label: 'Species Name' }, { key: 'family', label: 'Family' },
    { key: 'threat_score', label: 'IUCN Status' }, { key: 'threat_score', label: 'Threat Score' },
    { key: 'dna_level', label: 'Missing DNA Level' }, { key: 'priority', label: 'Priority' }
  ];

  function params() {
    const current = get(weights);
    return new URLSearchParams({
      search: get(tableSearch), sort: get(tableSort), order: get(tableOrder), page: String(get(tablePage)),
      ...Object.fromEntries(Object.entries(current).map(([key, value]) => [key, String(value)]))
    });
  }

  async function load() {
    loading = true; error = '';
    try { result = await api.species(params()); }
    catch (reason) { error = reason instanceof Error ? reason.message : 'Unable to load species'; }
    finally { loading = false; }
  }

  function schedule(resetPage = false) {
    if (resetPage) tablePage.set(1);
    clearTimeout(debounce);
    debounce = setTimeout(load, 120);
  }

  function sort(key: SortKey) {
    if (get(tableSort) === key) tableOrder.update((value) => value === 'asc' ? 'desc' : 'asc');
    else { tableSort.set(key); tableOrder.set(key === 'species_name' || key === 'family' ? 'asc' : 'desc'); }
    schedule(true);
  }

  function page(delta: number) {
    tablePage.update((value) => value + delta);
    load();
  }

  function keydown(event: KeyboardEvent) {
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || !result) return;
    if (event.key === 'ArrowLeft' && result.page > 1) page(-1);
    else if (event.key === 'ArrowRight' && result.page < result.total_pages) page(1);
    else {
      const shortcuts: Partial<Record<string, SortKey>> = {
        '1': 'species_name', '2': 'family', '3': 'threat_score', '4': 'dna_level', '5': 'priority'
      };
      const key = shortcuts[event.key];
      if (key) sort(key);
    }
  }

  function statusClass(category: string): string {
    return ({
      'Critically Endangered': 'status-cr', Endangered: 'status-en', Vulnerable: 'status-vu',
      'Near Threatened': 'status-nt', 'Least Concern': 'status-lc', 'Data Deficient': 'status-dd'
    } as Record<string, string>)[category] ?? '';
  }

  const unsubscribe = weights.subscribe(() => { if (result) schedule(true); });
  onMount(() => { load(); return () => { unsubscribe(); clearTimeout(debounce); }; });
</script>

<svelte:window on:keydown={keydown} />

<form class="search-form" on:submit|preventDefault={() => schedule(true)}>
  <input aria-label="Search species" type="text" placeholder="Search using REGEX..." bind:value={$tableSearch} />
  <button class="btn-primary" type="submit">Search</button>
</form>

{#if error}<p class="error-message">{error}</p>{/if}
<div class="table-scroll" aria-busy={loading}>
  <table id="species-table">
    <thead><tr>{#each columns as column}<th><button class="table-sort-link" type="button" on:click={() => sort(column.key)}>{column.label}{#if $tableSort === column.key} {$tableOrder === 'asc' ? '↑' : '↓'}{/if}</button></th>{/each}</tr></thead>
    <tbody>
      {#if loading && !result}<tr><td colspan="6" class="empty-row">Loading species…</td></tr>
      {:else if result?.rows?.length}
        {#each result?.rows ?? [] as row}
          <tr>
            <td>{row.species_name}</td><td>{row.family}</td><td><span class={`status-badge ${statusClass(row.redlist_category)}`}>{row.redlist_category}</span></td>
            <td>{row.threat_score.toFixed(2)}</td><td>{row.dna_level}</td><td>{row.priority.toFixed(2)}</td>
          </tr>
        {/each}
      {:else}<tr><td colspan="6" class="empty-row">No species match this search.</td></tr>{/if}
    </tbody>
  </table>
</div>

{#if result}
  <div class="pagination">
    <span class="page-info">Page {result.page} of {result.total_pages} · {result.total.toLocaleString()} species</span>
    <div class="pagination-buttons">
      <button class="btn-secondary" disabled={result.page <= 1} on:click={() => page(-1)}>Previous</button>
      <button class="btn-secondary" disabled={result.page >= result.total_pages} on:click={() => page(1)}>Next</button>
    </div>
  </div>
{/if}
