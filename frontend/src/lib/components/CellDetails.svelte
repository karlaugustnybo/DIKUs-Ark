<script lang="ts">
  import { tick } from 'svelte';
  import { api, type CellDetailsResponse, type CellSpeciesRow } from '$lib/api/client';
  import SpeciesDataTable from '$lib/components/SpeciesDataTable.svelte';
  import { dnaStatus, type SpeciesSortKey, type SpeciesTableRow } from '$lib/species-table';
  import {
    cellDetails, cellLoading, habitatSystem, resolution, selectedH3, selectedH3Resolution
  } from '$lib/stores/map';
  import { weights } from '$lib/stores/weights';

  type ScoredSpecies = CellSpeciesRow & SpeciesTableRow & { threat: number; dna: number };
  type BoundaryMembership = NonNullable<CellDetailsResponse['boundaries']>[number];
  type BoundaryGroup = {
    framework: string;
    label: string;
    memberships: BoundaryMembership[];
  };

  const locationFrameworks = [
    { framework: 'admin0', label: 'Country / territory' },
    { framework: 'admin1', label: 'State / region / province' },
    { framework: 'municipality', label: 'Municipality / local area' },
    { framework: 'conservation_framework', label: 'Conservation framework' }
  ];
  const hiddenLocationFrameworks = new Set(['eez']);

  let panel: HTMLElement;
  let page = 1;
  let sort: SpeciesSortKey = 'priority';
  let order: 'asc' | 'desc' = 'desc';
  let lastRequestKey = '';
  let error = '';
  let cellSearch = '';
  let redlistFilters: string[] = [];
  let dnaFilters: string[] = [];
  let perPage = 10;
  let cellAbortController: AbortController | null = null;
  let copyState: '' | 'copied' | 'failed' = '';
  let copyResetTimer: ReturnType<typeof setTimeout> | null = null;

  const statusRanks: Record<string, number> = {
    'Critically Endangered': 1, Endangered: 2, Vulnerable: 3,
    'Near Threatened': 4, 'Data Deficient': 5, 'Least Concern': 6, 'Not Assessed': 7
  };
  const dnaRanks: Record<string, number> = { 'Missing Family': 1, 'GoaT Data Deficient': 2, 'Missing Genus': 3, 'Missing Species': 4, Sampled: 5 };

  function enrich(species: CellSpeciesRow): ScoredSpecies {
    const threat = ({
      'Critically Endangered': $weights.cr, Endangered: $weights.en, Vulnerable: $weights.vu,
      'Near Threatened': $weights.nt, 'Data Deficient': $weights.dd, 'Least Concern': $weights.lc
    } as Record<string, number>)[species.redlist_category] ?? 0;
    const dna = species.dna_level === 'GoaT Data Deficient' ? $weights.gdd : species.dna_level === 'Missing Family' ? $weights.fam : species.dna_level === 'Missing Genus' ? $weights.gen : species.dna_level === 'Missing Species' ? $weights.sp : $weights.samp;
    const dnaLabel = species.dna_level === 'Sampled' ? 'Already Sampled' : `${species.dna_level} (${dna})`;
    return { ...species, threat, dna, dnaLabel, dnaStatus: dnaStatus(species.dna_level), priority: threat * dna };
  }

  function compare(a: ScoredSpecies, b: ScoredSpecies): number {
    let result = 0;
    if (sort === 'species_name' || sort === 'family') result = a[sort].localeCompare(b[sort]);
    else if (sort === 'redlist_category') result = (statusRanks[a.redlist_category] ?? 7) - (statusRanks[b.redlist_category] ?? 7);
    else if (sort === 'dna_level') result = a.dna - b.dna;
    else result = a.priority - b.priority;
    if (result) return order === 'asc' ? result : -result;
    if (sort === 'priority') {
      result = (statusRanks[a.redlist_category] ?? 7) - (statusRanks[b.redlist_category] ?? 7);
      if (!result) result = (dnaRanks[a.dna_level] ?? 5) - (dnaRanks[b.dna_level] ?? 5);
    } else if (sort === 'dna_level') result = (dnaRanks[a.dna_level] ?? 5) - (dnaRanks[b.dna_level] ?? 5);
    return result || a.species_name.localeCompare(b.species_name);
  }

  $: scored = ($cellDetails?.species ?? []).map(enrich).filter((species) => {
    const query = cellSearch.trim().toLocaleLowerCase();
    return (!query || `${species.species_name} ${species.family}`.toLocaleLowerCase().includes(query))
      && (!redlistFilters.length || redlistFilters.includes(species.redlist_category))
      && (!dnaFilters.length || dnaFilters.includes(species.dna_level));
  }).sort(compare);
  $: pages = Math.max(1, Math.ceil(scored.length / perPage));
  $: if (page > pages) page = pages;
  $: visible = scored.slice((page - 1) * perPage, page * perPage);
  $: displayResolution = $cellDetails?.resolution ?? cellResolution;
  $: boundaryGroups = (() => {
    const memberships = $cellDetails?.boundaries ?? [];
    const known = locationFrameworks.map(({ framework, label }) => ({
      framework,
      label,
      memberships: memberships.filter((item) => item.framework === framework)
    }));
    const knownIds = new Set(locationFrameworks.map((item) => item.framework));
    const additional = memberships.reduce<BoundaryGroup[]>((groups, item) => {
      if (knownIds.has(item.framework) || hiddenLocationFrameworks.has(item.framework)) return groups;
      const existing = groups.find((group) => group.framework === item.framework);
      if (existing) existing.memberships.push(item);
      else groups.push({ framework: item.framework, label: item.framework_name, memberships: [item] });
      return groups;
    }, []);
    return [...known, ...additional];
  })();

  async function load(h3: string, activeResolution: number, activeSystem: string) {
    cellAbortController?.abort();
    const controller = new AbortController();
    cellAbortController = controller;
    cellLoading.set(true);
    error = '';
    try {
      const details = await api.cell(
        h3,
        new URLSearchParams({ resolution: String(activeResolution), system: activeSystem }),
        controller.signal
      );
      if (controller.signal.aborted) return;
      cellDetails.set(details);
      page = 1;
      await tick();
      panel?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (reason) {
      if (controller.signal.aborted) return;
      error = reason instanceof Error ? reason.message : 'Failed to load cell.';
    } finally {
      if (cellAbortController === controller) {
        cellAbortController = null;
        cellLoading.set(false);
      }
    }
  }

  $: cellResolution = $selectedH3Resolution ?? $resolution;
  $: requestKey = $selectedH3 ? `${$selectedH3}:${cellResolution}:${$habitatSystem}` : '';
  $: if (requestKey && requestKey !== lastRequestKey && $selectedH3) {
    lastRequestKey = requestKey;
    copyState = '';
    if (copyResetTimer) clearTimeout(copyResetTimer);
    load($selectedH3, cellResolution, $habitatSystem);
  }
  $: if (!requestKey && cellAbortController) {
    cellAbortController.abort();
    cellAbortController = null;
    cellLoading.set(false);
  }

  function changeSort(next: SpeciesSortKey) {
    if (sort === next) order = order === 'asc' ? 'desc' : 'asc';
    else { sort = next; order = next === 'species_name' || next === 'family' || next === 'redlist_category' ? 'asc' : 'desc'; }
    page = 1;
  }

  function close() {
    cellAbortController?.abort();
    cellAbortController = null;
    if (copyResetTimer) clearTimeout(copyResetTimer);
    copyResetTimer = null;
    copyState = '';
    lastRequestKey = '';
    selectedH3.set(null);
    selectedH3Resolution.set(null);
    cellDetails.set(null);
  }

  async function copyCellId() {
    if (!$selectedH3) return;
    try {
      await navigator.clipboard.writeText($selectedH3);
      copyState = 'copied';
    } catch {
      copyState = 'failed';
    }
    if (copyResetTimer) clearTimeout(copyResetTimer);
    copyResetTimer = setTimeout(() => {
      copyState = '';
      copyResetTimer = null;
    }, 1800);
  }

  function toggleFilter(kind: 'redlist' | 'dna', value: string) {
    if (kind === 'redlist') redlistFilters = redlistFilters.includes(value) ? redlistFilters.filter((item) => item !== value) : [...redlistFilters, value];
    else dnaFilters = dnaFilters.includes(value) ? dnaFilters.filter((item) => item !== value) : [...dnaFilters, value];
    page = 1;
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
    <h2>
      <span>Cell</span>
      <button
        type="button"
        class="cell-id-copy"
        on:click={copyCellId}
        aria-label={`${copyState === 'copied' ? 'Copied' : 'Copy'} cell ID ${$selectedH3}`}
        title="Copy cell ID"
      >
        <code>{$selectedH3}</code>
        <small aria-live="polite">{copyState === 'copied' ? 'Copied' : copyState === 'failed' ? 'Failed' : 'Copy'}</small>
      </button>
      <small class="cell-resolution-badge">Resolution {displayResolution}</small>
    </h2>
    <button type="button" class="cell-close" on:click={close}>Close</button>
  </div>
  {#if $cellLoading}
    <span class="cell-loading">Loading…</span>
  {:else if error}
    <span class="cell-loading">{error}</span>
  {:else if $cellDetails}
    <section class="cell-context" aria-label="Cell location and boundary intersections">
      <dl class="cell-boundaries" aria-label="Cell location and all boundary intersections">
        {#each boundaryGroups as group}
          <div class="cell-boundary-group">
            <dt>{group.label}{#if group.memberships.length > 1} <span>{group.memberships.length}</span>{/if}</dt>
            <dd>
              {#if group.memberships.length}
                {#each group.memberships as membership, index}
                  {#if index}<span class="boundary-separator" aria-hidden="true">, </span>{/if}
                  <span class="boundary-membership" title={`${membership.framework_name}: ${membership.code}`}>{membership.name}</span>
                {/each}
              {:else}
                <span class="cell-boundary-none">No intersection</span>
              {/if}
            </dd>
          </div>
        {/each}
      </dl>
    </section>
    <div class="cell-stats">
      {#each [
        ['Total species', $cellDetails.stats?.total ?? 0], ['Critically Endangered', $cellDetails.stats?.CR ?? 0],
        ['Endangered', $cellDetails.stats?.EN ?? 0], ['Vulnerable', $cellDetails.stats?.VU ?? 0],
        ['Near Threatened', $cellDetails.stats?.NT ?? 0], ['Data Deficient', $cellDetails.stats?.DD ?? 0],
        ['Least Concern', $cellDetails.stats?.LC ?? 0], ['Missing species DNA', $cellDetails.stats?.missing_species_dna ?? 0],
        ['Missing genus DNA', $cellDetails.stats?.missing_genus_dna ?? 0], ['Missing family DNA', $cellDetails.stats?.missing_family_dna ?? 0],
        ['GoaT data deficient', $cellDetails.stats?.goat_data_deficient ?? 0]
      ] as stat}
        <div class="stat-card"><span class="stat-value">{stat[1]}</span><span class="stat-label">{stat[0]}</span></div>
      {/each}
    </div>
    <div class="cell-table-filters">
      <label><span class="sr-only">Filter species in this cell</span><input type="search" bind:value={cellSearch} placeholder="Filter this cell by species or family" /></label>
      <div class="cell-filter-row"><span>IUCN</span>{#each [['Critically Endangered','CR'],['Endangered','EN'],['Vulnerable','VU'],['Near Threatened','NT'],['Data Deficient','DD'],['Least Concern','LC']] as option}<button type="button" class:active={redlistFilters.includes(option[0])} on:click={() => toggleFilter('redlist', option[0])}>{option[1]}</button>{/each}</div>
      <div class="cell-filter-row"><span>DNA</span>{#each ['GoaT Data Deficient','Missing Family','Missing Genus','Missing Species','Sampled'] as option}<button type="button" class:active={dnaFilters.includes(option)} on:click={() => toggleFilter('dna', option)}>{option}</button>{/each}</div>
      <label class="page-size">Rows <select bind:value={perPage} on:change={() => page = 1}><option value={10}>10</option><option value={25}>25</option><option value={50}>50</option></select></label>
    </div>
    <SpeciesDataTable rows={visible} {sort} {order} loading={$cellLoading} onSort={changeSort} emptyMessage={cellSearch.trim() || redlistFilters.length || dnaFilters.length ? 'No species match these filters.' : 'No species records for this cell.'} />
    <div class="pagination" id="cell-pagination"><span class="page-info"><strong>{scored.length.toLocaleString()}</strong> of {($cellDetails.species ?? []).length.toLocaleString()} species · Page {page} of {pages}</span><div class="pagination-buttons"><button class="btn-secondary" disabled={page === 1} on:click={() => page--}>Previous</button><button class="btn-secondary" disabled={page === pages} on:click={() => page++}>Next</button></div></div>
  {/if}
</section>

<style>
  .cell-details-header h2 { display: flex; align-items: baseline; flex-wrap: wrap; gap: .35rem .6rem; }
  .cell-details-header h2 span { font-size: .72em; }
  .cell-id-copy { display: inline-flex; align-items: baseline; gap: .38rem; padding: .18rem .35rem; border: 1px solid transparent; border-radius: .3rem; background: transparent; color: var(--color-accent-brown); cursor: pointer; transition: background-color 140ms ease, border-color 140ms ease; }
  .cell-id-copy:hover { border-color: var(--color-gray-200); background: var(--color-primary-green-5); }
  .cell-id-copy:focus-visible { outline: 2px solid var(--color-primary-green); outline-offset: 2px; }
  .cell-id-copy code { font-size: .9rem; font-weight: 600; letter-spacing: .015em; }
  .cell-id-copy small { color: var(--color-gray-500); font-family: var(--font-sans); font-size: .52rem; font-weight: 750; letter-spacing: .05em; text-transform: uppercase; }
  .cell-resolution-badge { padding: .18rem .4rem; border: 1px solid var(--color-gray-200); border-radius: 999px; background: var(--color-primary-green-5); color: var(--color-gray-600); font-family: var(--font-sans); font-size: .58rem; font-weight: 750; letter-spacing: .035em; white-space: nowrap; }
  .cell-context { margin: 0 0 .85rem; padding: .58rem .72rem; border: 1px solid var(--color-gray-200); border-radius: var(--border-radius-base); background: color-mix(in srgb, var(--color-primary-green-5) 55%, var(--color-light-cream)); font-family: var(--font-sans); }
  .cell-boundary-group dt { color: var(--color-gray-500); font-size: .61rem; font-weight: 750; letter-spacing: .07em; line-height: 1.3; text-transform: uppercase; }
  .cell-boundaries { display: grid; gap: .2rem; max-height: 9rem; margin: 0; padding-right: .3rem; overflow-y: auto; scrollbar-width: thin; }
  .cell-boundary-group { display: grid; grid-template-columns: minmax(8.8rem, .34fr) minmax(0, 1.66fr); gap: .45rem; min-width: 0; }
  .cell-boundary-group dt { padding-top: .08rem; }
  .cell-boundary-group dt span { display: inline-grid; min-width: 1rem; height: 1rem; margin-left: .18rem; place-items: center; border-radius: 999px; background: var(--color-gray-200); color: var(--color-gray-600); font-size: .55rem; letter-spacing: 0; }
  .cell-boundary-group dd { display: inline; margin: 0; color: var(--color-gray-800); font-size: .7rem; line-height: 1.3; }
  .boundary-membership { min-width: 0; }
  .boundary-separator { color: var(--color-gray-400); }
  .cell-boundary-none { color: var(--color-gray-500); font-style: italic; }
  .cell-table-filters { display: grid; grid-template-columns: minmax(14rem, 1fr) 1.25fr 1.4fr auto; align-items: center; gap: .75rem; margin: 1rem 0 .75rem; padding: .75rem; border: 1px solid var(--color-gray-200); border-radius: var(--border-radius-base); background: var(--color-cream); font-family: var(--font-sans); }
  .cell-table-filters label input { width: 100%; padding: .55rem .65rem; border: 1px solid var(--color-gray-300); border-radius: var(--border-radius-base); background: var(--color-light-cream); font-size: .75rem; }
  .cell-filter-row { display: flex; flex-wrap: wrap; align-items: center; gap: .25rem; }
  .cell-filter-row > span { margin-right: .2rem; color: var(--color-gray-600); font-size: .62rem; font-weight: 700; text-transform: uppercase; }
  .cell-filter-row button { padding: .28rem .42rem; border: 1px solid var(--color-gray-300); border-radius: 999px; background: var(--color-light-cream); color: var(--color-gray-600); font-size: .61rem; }
  .cell-filter-row button.active { border-color: var(--color-primary-green); background: var(--color-primary-green-20); color: var(--color-accent-brown); }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
  @media (max-width: 1000px) { .cell-table-filters { grid-template-columns: 1fr; } }
  @media (max-width: 640px) { .cell-boundary-group { grid-template-columns: 1fr; gap: .05rem; } }
</style>
