<script lang="ts">
  import { tick } from 'svelte';
  import { api, type CellSpeciesRow } from '$lib/api/client';
  import { cellDetails, cellLoading, habitatSystem, resolution, selectedH3 } from '$lib/stores/map';
  import { weights } from '$lib/stores/weights';

  type SortKey = 'species_name' | 'family' | 'threat_score' | 'dna_level' | 'priority';
  type ScoredSpecies = CellSpeciesRow & { threat: number; dna: number; dnaLabel: string; priority: number };

  let panel: HTMLElement;
  let page = 1;
  let sort: SortKey = 'priority';
  let order: 'asc' | 'desc' = 'desc';
  let lastRequestKey = '';
  let error = '';
  const perPage = 10;

  const statusRanks: Record<string, number> = {
    'Critically Endangered': 1, Endangered: 2, Vulnerable: 3,
    'Near Threatened': 4, 'Data Deficient': 5, 'Least Concern': 6, 'Not Assessed': 7
  };
  const dnaRanks: Record<string, number> = { 'Missing Family': 1, 'Missing Genus': 2, 'Missing Species': 3, Sampled: 4 };

  function enrich(species: CellSpeciesRow): ScoredSpecies {
    const threat = ({
      'Critically Endangered': $weights.cr, Endangered: $weights.en, Vulnerable: $weights.vu,
      'Near Threatened': $weights.nt, 'Data Deficient': $weights.dd, 'Least Concern': $weights.lc
    } as Record<string, number>)[species.redlist_category] ?? 0;
    const dna = species.dna_level === 'Missing Family' ? $weights.fam : species.dna_level === 'Missing Genus' ? $weights.gen : species.dna_level === 'Missing Species' ? $weights.sp : $weights.samp;
    const dnaLabel = species.dna_level === 'Sampled' ? 'Already Sampled' : `${species.dna_level} (${dna})`;
    return { ...species, threat, dna, dnaLabel, priority: threat * dna };
  }

  function compare(a: ScoredSpecies, b: ScoredSpecies): number {
    let result = 0;
    if (sort === 'species_name' || sort === 'family') result = a[sort].localeCompare(b[sort]);
    else if (sort === 'threat_score') result = a.threat - b.threat;
    else if (sort === 'dna_level') result = a.dna - b.dna;
    else result = a.priority - b.priority;
    if (result) return order === 'asc' ? result : -result;
    if (sort === 'threat_score' || sort === 'priority') result = (statusRanks[a.redlist_category] ?? 7) - (statusRanks[b.redlist_category] ?? 7);
    else if (sort === 'dna_level') result = (dnaRanks[a.dna_level] ?? 4) - (dnaRanks[b.dna_level] ?? 4);
    return result || a.species_name.localeCompare(b.species_name);
  }

  $: scored = ($cellDetails?.species ?? []).map(enrich).sort(compare);
  $: pages = Math.max(1, Math.ceil(scored.length / perPage));
  $: if (page > pages) page = pages;
  $: visible = scored.slice((page - 1) * perPage, page * perPage);

  async function load(h3: string, activeResolution: number, activeSystem: string) {
    cellLoading.set(true);
    error = '';
    try {
      cellDetails.set(await api.cell(h3, new URLSearchParams({ resolution: String(activeResolution), system: activeSystem })));
      page = 1;
      await tick();
      panel?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'Failed to load cell.';
    } finally {
      cellLoading.set(false);
    }
  }

  $: requestKey = $selectedH3 ? `${$selectedH3}:${$resolution}:${$habitatSystem}` : '';
  $: if (requestKey && requestKey !== lastRequestKey && $selectedH3) {
    lastRequestKey = requestKey;
    load($selectedH3, $resolution, $habitatSystem);
  }

  function changeSort(next: SortKey) {
    if (sort === next) order = order === 'asc' ? 'desc' : 'asc';
    else { sort = next; order = 'asc'; }
    page = 1;
  }

  function arrow(key: SortKey): string {
    return sort === key ? (order === 'asc' ? ' ↑' : ' ↓') : '';
  }

  function close() {
    lastRequestKey = '';
    selectedH3.set(null);
    cellDetails.set(null);
  }

  function statusClass(category: string): string {
    return ({
      'Critically Endangered': 'status-cr', Endangered: 'status-en', Vulnerable: 'status-vu',
      'Near Threatened': 'status-nt', 'Least Concern': 'status-lc', 'Data Deficient': 'status-dd'
    } as Record<string, string>)[category] ?? '';
  }

  function keydown(event: KeyboardEvent) {
    if (!$selectedH3 || event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
    if (event.key === 'ArrowLeft' && page > 1) page -= 1;
    if (event.key === 'ArrowRight' && page < pages) page += 1;
  }
</script>

<svelte:window on:keydown={keydown} />

<section bind:this={panel} class:js-hidden={!$selectedH3} class="cell-details" aria-live="polite">
  <div class="cell-details-header">
    <h2>Cell {$selectedH3}</h2><button type="button" class="cell-close" on:click={close}>Close</button>
  </div>
  {#if $cellLoading}
    <span class="cell-loading">Loading…</span>
  {:else if error}
    <span class="cell-loading">{error}</span>
  {:else if $cellDetails}
    <div class="cell-stats">
      {#each [
        ['Total species', $cellDetails.stats?.total ?? 0], ['Critically Endangered', $cellDetails.stats?.CR ?? 0],
        ['Endangered', $cellDetails.stats?.EN ?? 0], ['Vulnerable', $cellDetails.stats?.VU ?? 0],
        ['Near Threatened', $cellDetails.stats?.NT ?? 0], ['Data Deficient', $cellDetails.stats?.DD ?? 0],
        ['Least Concern', $cellDetails.stats?.LC ?? 0], ['Missing species DNA', $cellDetails.stats?.missing_species_dna ?? 0],
        ['Missing genus DNA', $cellDetails.stats?.missing_genus_dna ?? 0], ['Missing family DNA', $cellDetails.stats?.missing_family_dna ?? 0]
      ] as stat}
        <div class="stat-card"><span class="stat-value">{stat[1]}</span><span class="stat-label">{stat[0]}</span></div>
      {/each}
    </div>
    <div class="cell-table-wrapper"><table class="cell-species-table">
      <thead><tr>
        <th><button class="cell-sort-link" on:click={() => changeSort('species_name')}>Species Name{arrow('species_name')}</button></th>
        <th><button class="cell-sort-link" on:click={() => changeSort('family')}>Family{arrow('family')}</button></th>
        <th><button class="cell-sort-link" on:click={() => changeSort('threat_score')}>IUCN Status{arrow('threat_score')}</button></th>
        <th><button class="cell-sort-link" on:click={() => changeSort('threat_score')}>Threat Score{arrow('threat_score')}</button></th>
        <th><button class="cell-sort-link" on:click={() => changeSort('dna_level')}>Missing DNA Level{arrow('dna_level')}</button></th>
        <th><button class="cell-sort-link" on:click={() => changeSort('priority')}>Priority{arrow('priority')}</button></th>
      </tr></thead>
      <tbody>
        {#each visible as species}
          <tr><td><em>{species.species_name}</em></td><td>{species.family}</td><td><span class={`status-badge ${statusClass(species.redlist_category)}`}>{species.redlist_category}</span></td><td>{species.threat.toFixed(2)}</td><td>{species.dnaLabel}</td><td class="priority-cell">{species.priority.toFixed(2)}</td></tr>
        {:else}
          <tr><td colspan="6" class="cell-empty">No species records for this cell.</td></tr>
        {/each}
      </tbody>
    </table></div>
    {#if pages > 1}<div class="pagination" id="cell-pagination"><span class="page-info">Page {page} of {pages}</span><div class="pagination-buttons"><button class="btn-secondary" disabled={page === 1} on:click={() => page--}>Previous</button><button class="btn-secondary" disabled={page === pages} on:click={() => page++}>Next</button></div></div>{/if}
  {/if}
</section>
