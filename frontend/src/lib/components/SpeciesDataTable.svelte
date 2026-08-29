<script lang="ts">
  import SourceLink from '$lib/components/SourceLink.svelte';
  import { gbifSpeciesUrl, goatTaxonUrl, iucnSpeciesUrl } from '$lib/sourceLinks';
  import type { SpeciesSortKey, SpeciesTableRow } from '$lib/species-table';

  export let rows: SpeciesTableRow[] = [];
  export let sort: SpeciesSortKey = 'priority';
  export let order: 'asc' | 'desc' = 'desc';
  export let loading = false;
  export let emptyMessage = 'No species match these filters.';
  export let onSort: (key: SpeciesSortKey) => void;

  const columns: Array<{ key: SpeciesSortKey; label: string; numeric?: boolean }> = [
    { key: 'species_name', label: 'Species' },
    { key: 'family', label: 'Family' },
    { key: 'redlist_category', label: 'IUCN status' },
    { key: 'dna_level', label: 'DNA gap' },
    { key: 'priority', label: 'Priority', numeric: true }
  ];

  function statusClass(category: string): string {
    return ({
      'Critically Endangered': 'status-cr', Endangered: 'status-en', Vulnerable: 'status-vu',
      'Near Threatened': 'status-nt', 'Least Concern': 'status-lc', 'Data Deficient': 'status-dd'
    } as Record<string, string>)[category] ?? 'status-na';
  }

  function ariaSort(key: SpeciesSortKey): 'ascending' | 'descending' | 'none' {
    return sort === key ? (order === 'asc' ? 'ascending' : 'descending') : 'none';
  }

  function chooseSort(event: Event) {
    const next = (event.currentTarget as HTMLSelectElement).value as SpeciesSortKey;
    if (next !== sort) onSort(next);
  }
</script>

<div class="species-table-frame" aria-busy={loading}>
  <div class="mobile-table-sort">
    <label>Sort by <select value={sort} on:change={chooseSort}>{#each columns as column}<option value={column.key}>{column.label}</option>{/each}</select></label>
    <button class="btn-secondary" type="button" on:click={() => onSort(sort)} aria-label={`Sort ${order === 'asc' ? 'descending' : 'ascending'}`}>
      {order === 'asc' ? 'Ascending ↑' : 'Descending ↓'}
    </button>
  </div>
  <div class="table-scroll" role="region" aria-label="Species results">
    <table class="species-data-table">
      <colgroup>
        <col class="species-column" />
        <col class="family-column" />
        <col class="status-column" />
        <col class="dna-column" />
        <col class="priority-column" />
      </colgroup>
      <thead><tr>{#each columns as column}<th class:numeric-column={column.numeric} aria-sort={ariaSort(column.key)}><button class="table-sort-link" type="button" on:click={() => onSort(column.key)}><span>{column.label}</span><span class:sort-active={sort === column.key} class="sort-indicator" aria-hidden="true">{sort === column.key ? (order === 'asc' ? '↑' : '↓') : '↕'}</span></button></th>{/each}</tr></thead>
      <tbody>
        {#if loading && !rows.length}
          <tr><td colspan="5" class="empty-row">Loading species…</td></tr>
        {:else if rows.length}
          {#each rows as row}
            {@const gbifHref = gbifSpeciesUrl(row.gbif_taxon_id)}
            {@const goatHref = goatTaxonUrl(row.goat_taxon_id)}
            <tr>
              <td data-label="Species" class="species-name">{#if gbifHref}<SourceLink source="GBIF" href={gbifHref} context={row.species_name} fused className="species-source-link"><em>{row.species_name}</em></SourceLink>{:else}<em>{row.species_name}</em>{/if}</td>
              <td data-label="Family">{row.family || '—'}</td>
              <td data-label="IUCN status"><SourceLink source="IUCN" href={iucnSpeciesUrl(row.species_name, row.iucn_sis_id, row.iucn_assessment_id)} context={row.species_name} fused className={`status-badge ${statusClass(row.redlist_category)}`}>{row.redlist_category}</SourceLink></td>
              <td data-label="DNA gap">{#if goatHref}<SourceLink source="GoaT" href={goatHref} context={row.species_name} fused className={`dna-badge dna-${row.dnaStatus}`}>{row.dnaLabel}</SourceLink>{:else}<span class={`dna-badge dna-${row.dnaStatus}`}>{row.dnaLabel}</span>{/if}</td>
              <td data-label="Priority" class="score-cell"><span class="score-value priority-value">{row.priority.toFixed(2)}</span></td>
            </tr>
          {/each}
        {:else}
          <tr><td colspan="5" class="empty-row">{emptyMessage}</td></tr>
        {/if}
      </tbody>
    </table>
  </div>
</div>
