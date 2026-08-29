<script lang="ts">
  import { onMount } from 'svelte';
  import { Globe2, Leaf, Locate, LocateFixed, MapPinned, ShieldCheck, Waves } from 'lucide-svelte';
  import {
    boundaryCollections,
    boundaryFilters,
    boundaryFrameworks,
    boundaryLoadError,
    clearBoundaryFilters,
    loadBoundaryCollection,
    loadBoundaryFrameworks,
    toggleBoundary,
    type BoundaryFramework
  } from '$lib/stores/boundaries';

  export let condensed = false;
  export let showSummary = true;
  const MAX_SELECTIONS = 30;
  const INITIAL_RESULTS = 72;
  const RESULT_STEP = 96;
  let open = false;
  let activeFramework = 'admin0';
  let query = '';
  let resultLimit = INITIAL_RESULTS;
  let resultContext = '';
  let loadingFramework = '';
  let browseAllFrameworks: string[] = [];
  const searchTextCache = new Map<string, string>();
  const frameworkIcons = {
    admin0: Globe2,
    admin1: MapPinned,
    municipality: Locate,
    eez: Waves,
    protected_area: ShieldCheck,
    conservation_framework: Leaf
  };

  function setOpen(nextOpen: boolean) {
    open = nextOpen;
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && open) setOpen(false);
  }

  $: framework = $boundaryFrameworks.find((item) => item.id === activeFramework);
  $: filterableFrameworks = $boundaryFrameworks.filter((item) => item.filterable !== false);
  $: ActiveFrameworkIcon = frameworkIcons[activeFramework as keyof typeof frameworkIcons] ?? LocateFixed;
  $: parentFramework = framework?.parent_framework
    ? $boundaryFrameworks.find((item) => item.id === framework?.parent_framework)
    : undefined;
  $: ParentFrameworkIcon = frameworkIcons[(framework?.parent_framework ?? 'admin0') as keyof typeof frameworkIcons] ?? LocateFixed;
  $: collection = $boundaryCollections[activeFramework];
  $: selected = $boundaryFilters[activeFramework] ?? [];
  $: selectedSet = new Set(selected);
  $: totalSelected = Object.values($boundaryFilters).reduce((total, values) => total + values.length, 0);
  $: parentSelections = framework?.parent_framework ? ($boundaryFilters[framework.parent_framework] ?? []) : [];
  $: needsParentSelection = Boolean(
    framework?.catalog_partition_url &&
    !parentSelections.length &&
    !selected.length &&
    !browseAllFrameworks.includes(activeFramework)
  );
  $: normalizedQuery = query.trim().toLocaleLowerCase();
  $: options = (collection?.features ?? []).filter((feature) => {
    const properties = feature.properties;
    const matchesParent = !parentSelections.length || !properties.parent_code || parentSelections.includes(properties.parent_code);
    const cacheKey = `${activeFramework}:${properties.code}`;
    let haystack = searchTextCache.get(cacheKey);
    if (!haystack) {
      haystack = `${properties.name} ${properties.code} ${properties.continent ?? ''} ${properties.region ?? ''} ${properties.boundary_type ?? ''} ${properties.biome ?? ''} ${properties.conservation_status ?? ''}`.toLocaleLowerCase();
      searchTextCache.set(cacheKey, haystack);
    }
    return matchesParent && (!normalizedQuery || haystack.includes(normalizedQuery));
  }).sort((left, right) => {
    const selectedDifference = Number(selectedSet.has(right.properties.code)) - Number(selectedSet.has(left.properties.code));
    return selectedDifference;
  });
  $: nextResultContext = `${activeFramework}|${normalizedQuery}|${parentSelections.join(',')}|${collection?.features.length ?? 0}`;
  $: if (nextResultContext !== resultContext) {
    resultContext = nextResultContext;
    resultLimit = INITIAL_RESULTS;
  }
  $: visibleOptions = options.slice(0, resultLimit);
  $: hiddenResultCount = Math.max(0, options.length - visibleOptions.length);

  async function loadFramework(item: BoundaryFramework, parents: string[] = []) {
    loadingFramework = item.id;
    try {
      await loadBoundaryCollection(item.id, parents);
    } finally {
      if (loadingFramework === item.id) loadingFramework = '';
    }
  }

  function activate(item: BoundaryFramework) {
    activeFramework = item.id;
    query = '';
    const parents = item.parent_framework ? ($boundaryFilters[item.parent_framework] ?? []) : [];
    const shouldLoad = !item.catalog_partition_url || parents.length || browseAllFrameworks.includes(item.id);
    if (item.status === 'ready' && shouldLoad) {
      void loadFramework(item, browseAllFrameworks.includes(item.id) ? [] : parents).catch(() => undefined);
    }
  }

  function browseAll() {
    if (!framework) return;
    browseAllFrameworks = [...new Set([...browseAllFrameworks, framework.id])];
    void loadFramework(framework).catch(() => undefined);
  }

  function selectVisible() {
    boundaryFilters.update((filters) => ({
      ...filters,
      [activeFramework]: [...new Set([
        ...(filters[activeFramework] ?? []),
        ...options.map((feature) => feature.properties.code)
      ])].slice(0, MAX_SELECTIONS)
    }));
  }

  function selectedLabel(frameworkId: string, code: string): string {
    return $boundaryCollections[frameworkId]?.features.find((feature) => feature.properties.code === code)?.properties.name ?? code;
  }

  onMount(() => {
    void (async () => {
      try {
        await loadBoundaryFrameworks();
        await loadBoundaryCollection(activeFramework);
      } catch { /* The inline error state is sufficient. */ }
    })();
  });

  $: if (
    framework?.status === 'ready' &&
    framework.catalog_partition_url &&
    parentSelections.length &&
    !browseAllFrameworks.includes(activeFramework)
  ) {
    void loadFramework(framework, parentSelections).catch(() => undefined);
  }
</script>

<div class:condensed class="boundary-filter sliders-dropdown">
  <button
    type="button"
    class:is-open={open}
    class:has-selection={totalSelected > 0}
    class="dropdown-toggle boundary-toggle"
    on:click={() => setOpen(!open)}
    aria-expanded={open}
    aria-controls="boundary-filter-panel"
  >
    <span class="boundary-mark" aria-hidden="true"><LocateFixed size={15} strokeWidth={1.8} /></span>
    <span>{totalSelected ? `${totalSelected} place filter${totalSelected === 1 ? '' : 's'}` : 'Filter by place & framework'}</span>
    {#if totalSelected}<span class="selection-count">{totalSelected}</span>{/if}
    <svg class="chevron-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m19.5 8.25-7.5 7.5-7.5-7.5" /></svg>
  </button>

  <div
    id="boundary-filter-panel"
    class:js-hidden={!open}
    class="boundary-panel dropdown-panel"
    aria-label="Spatial scope filters"
  >
      <header class="boundary-heading">
        <div><span class="control-kicker">Spatial scope</span><strong>Boundary frameworks</strong></div>
        {#if totalSelected}<button type="button" class="text-action" on:click={() => clearBoundaryFilters()}>Clear all</button>{/if}
      </header>

      <div class="framework-rail" role="tablist" aria-label="Boundary frameworks">
        {#each filterableFrameworks as item (item.id)}
          <button
            type="button"
            role="tab"
            aria-selected={activeFramework === item.id}
            class:active={activeFramework === item.id}
            class:unavailable={item.status !== 'ready'}
            class="framework-tab"
            on:click={() => activate(item)}
          >
            <span class="framework-icon" style:--swatch={`rgb(${item.color.join(',')})`} aria-hidden="true">
              <svelte:component this={frameworkIcons[item.id as keyof typeof frameworkIcons] ?? LocateFixed} size={15} strokeWidth={1.8} />
            </span>
            <span>{item.short_name}<small>{item.group}</small></span>
            {#if ($boundaryFilters[item.id] ?? []).length}<b>{($boundaryFilters[item.id] ?? []).length}</b>{/if}
          </button>
        {/each}
      </div>

      {#if framework?.status === 'source-required'}
        <div class="source-required">
          <span class="source-icon" aria-hidden="true"><svelte:component this={ActiveFrameworkIcon} size={23} strokeWidth={1.55} /></span>
          <div><strong>{framework.name}</strong><p>{framework.description}</p><small>{framework.import_hint ?? 'The filtering pipeline is prepared; an approved source package must be configured before this layer can be enabled.'}</small>{#if framework.source_url}<a href={framework.source_url} target="_blank" rel="noreferrer">Open official source ↗</a>{/if}</div>
        </div>
      {:else if framework}
        <div class="framework-intro"><strong>{framework.name}</strong><span>{framework.description}</span></div>
        {#if framework.coverage_note}<div class="coverage-note">{framework.coverage_note}</div>{/if}
        {#if needsParentSelection}
          <div class="scope-gate">
            <span class="scope-gate-mark" aria-hidden="true"><svelte:component this={ParentFrameworkIcon} size={21} strokeWidth={1.6} /></span>
            <div>
              <strong>Start with {parentFramework?.short_name.toLocaleLowerCase() ?? 'a parent area'}</strong>
              <p>Choose one or more {parentFramework?.short_name.toLocaleLowerCase() ?? 'parent areas'} first and Ark-IV will load only their {framework.short_name.toLocaleLowerCase()}.</p>
              <div class="scope-gate-actions">
                {#if parentFramework}<button type="button" class="scope-primary" on:click={() => activate(parentFramework)}>Choose {parentFramework.short_name.toLocaleLowerCase()}</button>{/if}
                <button type="button" class="text-action" on:click={browseAll}>Browse all instead</button>
              </div>
            </div>
          </div>
        {:else}
        {#if parentSelections.length}
          <div class="parent-note">Showing divisions inside {parentSelections.length} selected {parentSelections.length === 1 ? 'country' : 'countries'}.</div>
        {/if}
        {#if loadingFramework === activeFramework && collection}<div class="catalogue-progress"><span></span>Adding boundary catalogue…</div>{/if}
        <label class="boundary-search">
          <span class="sr-only">Search {framework.name}</span>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/></svg>
          <input bind:value={query} type="search" placeholder={`Search ${framework.short_name.toLocaleLowerCase()}`} autocomplete="off" />
        </label>
        <div class="boundary-meta">
          <span>{options.length.toLocaleString()} {options.length === 1 ? 'result' : 'results'}</span>
          <div>{#if selected.length}<button type="button" class="text-action" on:click={() => clearBoundaryFilters(activeFramework)}>Clear layer</button>{/if}<button type="button" class="text-action" on:click={selectVisible} disabled={!options.length || selected.length >= MAX_SELECTIONS}>Select visible</button></div>
        </div>
        {#if $boundaryLoadError && !collection}
          <p class="error-message">{$boundaryLoadError}</p>
        {:else if !collection || loadingFramework === activeFramework && !collection}
          <p class="boundary-empty">Loading boundaries…</p>
        {:else}
          <div class="boundary-options" role="group" aria-label={framework.name}>
            {#each visibleOptions as feature (feature.properties.code)}
              <label class:selected={selected.includes(feature.properties.code)} class="boundary-option">
                <input type="checkbox" checked={selected.includes(feature.properties.code)} disabled={!selected.includes(feature.properties.code) && selected.length >= MAX_SELECTIONS} on:change={() => toggleBoundary(activeFramework, feature.properties.code)} />
                <span class="custom-check" aria-hidden="true"></span>
                <span class="boundary-name">{feature.properties.name}<small>{feature.properties.biome ?? feature.properties.boundary_type ?? feature.properties.region ?? feature.properties.continent ?? 'Administrative area'}</small></span>
                <span class="boundary-code">{feature.properties.code}</span>
              </label>
            {:else}<p class="boundary-empty">No boundaries match “{query}”.</p>{/each}
            {#if hiddenResultCount}
              <button type="button" class="show-more" on:click={() => (resultLimit += RESULT_STEP)}>
                <span>Show {Math.min(RESULT_STEP, hiddenResultCount).toLocaleString()} more</span>
                <small>{hiddenResultCount.toLocaleString()} remaining</small>
              </button>
            {/if}
          </div>
        {/if}
        <footer class="boundary-footnote"><span>Includes every cell that touches the boundary.</span><span>{Math.max(0, MAX_SELECTIONS - selected.length)} selections remaining in this layer</span></footer>
        {/if}
      {/if}
  </div>

  {#if showSummary}
    <div class:has-selection={totalSelected > 0} class="boundary-summary" aria-live="polite">
      <span class="summary-dot" aria-hidden="true"></span>
      {#if totalSelected}
        <span><strong>Scoped view</strong> · {Object.entries($boundaryFilters).flatMap(([id, codes]) => codes.map((code) => selectedLabel(id, code))).slice(0, 3).join(', ')}{totalSelected > 3 ? ` +${totalSelected - 3}` : ''}</span>
      {:else}<span><strong>Worldwide</strong> · no spatial filters</span>{/if}
    </div>
  {/if}
</div>

<svelte:window on:keydown={handleKeydown} />

<style>
  .boundary-filter { width: 100%; margin: var(--space-24) auto 0; font-family: var(--font-sans); }
  .boundary-filter.condensed { margin: 0; max-width: none; }
  .boundary-toggle { min-width: 17rem; justify-content: center; }
  .boundary-toggle.has-selection { border-color: var(--color-primary-green); background: var(--color-primary-green-5); }
  .boundary-mark { display: grid; place-items: center; width: 1rem; height: 1rem; color: var(--color-tertiary-green); }
  .selection-count { display: inline-grid; place-items: center; min-width: 1.35rem; height: 1.35rem; padding: 0 .28rem; border-radius: 999px; background: var(--color-primary-green-20); color: var(--color-accent-brown); font-size: .72rem; }
  .boundary-panel { width: 100%; text-align: left; overflow: hidden; }
  .boundary-panel.js-hidden { display: none; }
  .boundary-heading { display: flex; justify-content: space-between; align-items: center; padding: 1rem 1.2rem .75rem; }
  .boundary-heading strong { display: block; color: var(--color-gray-800); font-size: 1rem; }
  .control-kicker { display: block; color: var(--color-gray-600); font-family: var(--font-mono); font-size: .66rem; letter-spacing: .09em; text-transform: uppercase; }
  .text-action { padding: .25rem .35rem; color: var(--color-accent-brown); font-size: .74rem; font-weight: 650; }
  .text-action:disabled { color: var(--color-gray-400); cursor: default; }
  .framework-rail { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; padding: 1px; background: var(--color-gray-200); border-block: 1px solid var(--color-gray-200); }
  .framework-tab { display: grid; grid-template-columns: 1.15rem 1fr auto; align-items: center; gap: .5rem; padding: .6rem .7rem; border-radius: 0; background: var(--color-cream); color: var(--color-gray-700); text-align: left; font-size: .73rem; }
  .framework-tab:hover { background: var(--color-primary-green-5); }
  .framework-tab.active { background: var(--color-light-cream); color: var(--color-accent-brown); box-shadow: inset 0 -2px 0 var(--swatch, var(--color-primary-green)); }
  .framework-tab.unavailable { color: var(--color-gray-500); }
  .framework-tab small { display: block; color: var(--color-gray-500); font-size: .6rem; }
  .framework-tab b { min-width: 1.2rem; padding: .08rem .3rem; border-radius: 999px; background: var(--color-primary-green-10); text-align: center; font-size: .62rem; }
  .framework-icon { display: grid; place-items: center; width: 1.15rem; height: 1.15rem; color: var(--swatch); }
  .framework-intro { display: flex; flex-direction: column; gap: .12rem; padding: .8rem 1.2rem .55rem; }
  .framework-intro strong { color: var(--color-gray-800); font-size: .85rem; }
  .framework-intro span { color: var(--color-gray-600); font-family: var(--font-body); font-size: .75rem; }
  .parent-note { margin: 0 1.2rem .55rem; padding: .4rem .55rem; border-left: 2px solid var(--color-primary-green); background: var(--color-primary-green-5); color: var(--color-gray-600); font-size: .7rem; }
  .coverage-note { margin: 0 1.2rem .55rem; color: var(--color-gray-500); font-size: .66rem; }
  .scope-gate { display: grid; grid-template-columns: 2.8rem 1fr; gap: 1rem; align-items: start; margin: .15rem 1.2rem 1.1rem; padding: 1.15rem; border: 1px solid var(--color-gray-200); border-radius: var(--border-radius-base); background: linear-gradient(135deg, var(--color-light-cream), var(--color-primary-green-5)); }
  .scope-gate-mark { display: grid; place-items: center; width: 2.8rem; height: 2.8rem; border: 1px solid var(--color-primary-green-20); border-radius: 50%; color: var(--color-tertiary-green); }
  .scope-gate strong { color: var(--color-gray-800); font-size: .84rem; }
  .scope-gate p { margin: .25rem 0 .7rem; color: var(--color-gray-600); font-family: var(--font-body); font-size: .75rem; line-height: 1.45; }
  .scope-gate-actions { display: flex; align-items: center; gap: .7rem; }
  .scope-primary { padding: .48rem .72rem; border-radius: var(--border-radius-base); background: var(--color-primary-green); color: var(--color-light-cream); font-size: .7rem; font-weight: 650; }
  .catalogue-progress { display: flex; align-items: center; gap: .45rem; margin: 0 1.2rem .55rem; color: var(--color-gray-500); font-size: .66rem; }
  .catalogue-progress span { width: .42rem; height: .42rem; border-radius: 50%; background: var(--color-primary-green); animation: catalogue-pulse 1s ease-in-out infinite alternate; }
  .source-required { display: flex; gap: 1rem; min-height: 15rem; align-items: center; padding: 2rem 2.5rem; background: linear-gradient(135deg, var(--color-cream), var(--color-primary-green-5)); }
  .source-icon { display: grid; place-items: center; flex: 0 0 3rem; height: 3rem; border: 1px solid var(--color-gray-300); border-radius: 50%; color: var(--color-gray-500); }
  .source-required strong { color: var(--color-gray-800); }
  .source-required p { margin: .35rem 0; color: var(--color-gray-600); font-size: .85rem; }
  .source-required small { color: var(--color-gray-500); font-size: .7rem; }
  .source-required a { display: inline-block; margin-top: .65rem; color: var(--color-accent-brown); font-size: .7rem; font-weight: 650; }
  .boundary-search { display: flex; align-items: center; gap: .6rem; margin: 0 1.2rem; padding: .62rem .75rem; border: 1px solid var(--color-gray-300); border-radius: var(--border-radius-base); color: var(--color-gray-500); background: var(--color-cream); }
  .boundary-search:focus-within { border-color: var(--color-primary-green); box-shadow: 0 0 0 3px var(--color-primary-green-10); }
  .boundary-search input { width: 100%; border: 0; outline: 0; background: transparent; color: var(--color-black); font-size: .84rem; }
  .boundary-meta { display: flex; justify-content: space-between; align-items: center; padding: .48rem 1.2rem .38rem; color: var(--color-gray-600); font-size: .7rem; }
  .boundary-meta div { display: flex; gap: .35rem; }
  .boundary-options { max-height: 18rem; overflow-y: auto; overscroll-behavior: contain; touch-action: pan-y; -webkit-overflow-scrolling: touch; border-block: 1px solid var(--color-gray-200); }
  .boundary-option { position: relative; display: grid; grid-template-columns: 1.05rem 1fr auto; align-items: center; gap: .7rem; padding: .55rem 1.2rem; cursor: pointer; transition: background .15s ease; }
  .boundary-option:hover, .boundary-option.selected { background: var(--color-primary-green-5); }
  .boundary-option input { position: absolute; opacity: 0; pointer-events: none; }
  .custom-check { width: 1rem; height: 1rem; border: 1px solid var(--color-gray-400); border-radius: .25rem; background: var(--color-light-cream); }
  .boundary-option input:checked + .custom-check { border-color: var(--color-tertiary-green); background: var(--color-primary-green); box-shadow: inset 0 0 0 3px var(--color-light-cream); }
  .boundary-name { color: var(--color-gray-800); font-size: .8rem; line-height: 1.12; }
  .boundary-name small { display: block; margin-top: .12rem; color: var(--color-gray-600); font-size: .64rem; }
  .boundary-code { color: var(--color-gray-500); font-family: var(--font-mono); font-size: .64rem; }
  .show-more { display: flex; justify-content: space-between; align-items: center; width: 100%; padding: .7rem 1.2rem; border-radius: 0; border-top: 1px solid var(--color-gray-200); background: var(--color-light-cream); color: var(--color-accent-brown); font-size: .72rem; font-weight: 650; }
  .show-more:hover { background: var(--color-primary-green-5); }
  .show-more small { color: var(--color-gray-500); font-size: .62rem; font-weight: 500; }
  .boundary-empty { padding: 1.6rem; color: var(--color-gray-600); font-size: .8rem; text-align: center; }
  .boundary-footnote { display: flex; justify-content: space-between; gap: 1rem; padding: .6rem 1.2rem; color: var(--color-gray-500); font-size: .64rem; }
  .boundary-summary { display: flex; justify-content: center; align-items: center; gap: .4rem; margin-top: .5rem; color: var(--color-gray-600); font-size: .7rem; }
  .boundary-summary strong { color: var(--color-gray-700); }
  .summary-dot { width: .36rem; height: .36rem; border-radius: 50%; background: var(--color-gray-400); }
  .boundary-summary.has-selection .summary-dot { background: var(--color-primary-green); box-shadow: 0 0 0 3px var(--color-primary-green-10); }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
  @keyframes catalogue-pulse { to { opacity: .35; transform: scale(.72); } }
  @media (max-width: 650px) {
    .boundary-toggle { width: 100%; min-width: 0; }
    .framework-rail { grid-template-columns: repeat(2, 1fr); }
    .boundary-options { max-height: 18rem; }
    .boundary-footnote { flex-direction: column; gap: .15rem; }
  }
</style>
