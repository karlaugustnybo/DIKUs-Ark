<script lang="ts">
  import { onDestroy, tick } from 'svelte';
  import { Search, X } from 'lucide-svelte';
  import { api, type SpeciesSuggestion } from '$lib/api/client';
  import { selectedSpecies, speciesHighlight, speciesHighlightGradient } from '$lib/stores/species';

  let expanded = false;
  let inputEl: HTMLInputElement | undefined;
  let query = '';
  let results: SpeciesSuggestion[] = [];
  let loading = false;
  let error = '';
  let open = false;
  let activeIndex = -1;
  let searchTimer: ReturnType<typeof setTimeout>;
  let requestId = 0;
  let abortController: AbortController | null = null;
  const resultCache = new Map<string, SpeciesSuggestion[]>();
  const CACHE_SIZE = 24;

  function normalizedQuery() {
    return query.trim().replace(/\s+/g, ' ');
  }

  function cache(queryKey: string, rows: SpeciesSuggestion[]) {
    resultCache.delete(queryKey);
    resultCache.set(queryKey, rows);
    if (resultCache.size > CACHE_SIZE) {
      const oldest = resultCache.keys().next().value;
      if (oldest !== undefined) resultCache.delete(oldest);
    }
  }

  function scheduleSearch() {
    clearTimeout(searchTimer);
    abortController?.abort();
    abortController = null;
    requestId += 1;
    activeIndex = -1;
    error = '';
    if ($selectedSpecies && query !== $selectedSpecies.species_name) selectedSpecies.set(null);
    const nextQuery = normalizedQuery();
    if (nextQuery.length < 2) {
      results = [];
      loading = false;
      open = false;
      return;
    }
    const cached = resultCache.get(nextQuery.toLocaleLowerCase());
    if (cached) {
      results = cached;
      loading = false;
      open = true;
      return;
    }
    loading = true;
    open = true;
    searchTimer = setTimeout(() => search(nextQuery), 180);
  }

  async function search(searchQuery: string) {
    const currentRequest = ++requestId;
    const controller = new AbortController();
    abortController = controller;
    try {
      const response = await api.speciesSuggestions(searchQuery, 8, controller.signal);
      if (currentRequest !== requestId) return;
      results = response.rows ?? [];
      cache(searchQuery.toLocaleLowerCase(), results);
    } catch (reason) {
      if (currentRequest === requestId && !(reason instanceof DOMException && reason.name === 'AbortError')) {
        results = [];
        error = reason instanceof Error ? reason.message : 'Search unavailable';
      }
    } finally {
      if (currentRequest === requestId) loading = false;
      if (abortController === controller) abortController = null;
    }
  }

  function select(row: SpeciesSuggestion) {
    query = row.species_name;
    speciesHighlightGradient.set(false);
    selectedSpecies.set({ gbif_accepted_id: row.gbif_accepted_id, species_name: row.species_name });
    results = [];
    open = false;
    activeIndex = -1;
  }

  function clear() {
    requestId += 1;
    clearTimeout(searchTimer);
    abortController?.abort();
    abortController = null;
    query = '';
    results = [];
    open = false;
    loading = false;
    error = '';
    selectedSpecies.set(null);
  }

  function collapse() {
    expanded = false;
    open = false;
    activeIndex = -1;
  }

  async function expand() {
    expanded = true;
    await tick();
    inputEl?.focus();
    if (query.trim().length >= 2 && !$selectedSpecies) open = true;
  }

  function windowKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && expanded && !(event.target instanceof HTMLInputElement)) collapse();
  }

  function keydown(event: KeyboardEvent) {
    if (!open || !results.length) {
      if (event.key === 'Escape' && $selectedSpecies) clear();
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault(); activeIndex = (activeIndex + 1) % results.length;
    } else if (event.key === 'ArrowUp') {
      event.preventDefault(); activeIndex = (activeIndex - 1 + results.length) % results.length;
    } else if (event.key === 'Enter' && activeIndex >= 0) {
      event.preventDefault(); select(results[activeIndex]);
    } else if (event.key === 'Escape') {
      if (open) { open = false; activeIndex = -1; } else collapse();
    }
  }

  function focusout(event: FocusEvent) {
    const next = event.relatedTarget;
    const section = event.currentTarget as HTMLElement | null;
    if (!(next instanceof Node) || !section?.contains(next)) {
      open = false; activeIndex = -1;
    }
  }

  onDestroy(() => {
    clearTimeout(searchTimer);
    requestId += 1;
    abortController?.abort();
    resultCache.clear();
  });
</script>

<section class="species-highlight" class:is-expanded={expanded} aria-label="Species distribution highlight" on:focusout={focusout}>
  {#if expanded}
    <div id="species-highlight-panel" class="species-panel">
      <button class="species-close" type="button" aria-label="Close species search" on:click={collapse}><X size={14} strokeWidth={2} /></button>
      <label for="species-highlight-input">Highlight a species</label>
      <div class="species-search">
        <span class="search-icon" aria-hidden="true"></span>
        <input bind:this={inputEl} id="species-highlight-input" type="search" maxlength="120" placeholder="Search species…" autocomplete="off" role="combobox" aria-autocomplete="list" aria-controls="species-highlight-results" aria-expanded={open} aria-activedescendant={activeIndex >= 0 ? `species-result-${activeIndex}` : undefined} bind:value={query} on:input={scheduleSearch} on:keydown={keydown} on:focus={() => { if (query.trim().length >= 2 && !$selectedSpecies) open = true; }} />
        {#if query}<button class="species-clear" type="button" aria-label="Clear species highlight" on:click={clear}>×</button>{/if}
      </div>
      {#if open}
        <div id="species-highlight-results" class="species-results" role="listbox">
          {#if loading}<div class="species-result-message">Searching…</div>
          {:else if error}<div class="species-result-message is-error" role="status">{error}</div>
          {:else if results.length}{#each results as row, index}<button id={`species-result-${index}`} type="button" role="option" aria-selected={activeIndex === index} class:is-active={activeIndex === index} on:mousedown|preventDefault={() => select(row)}><em>{row.species_name}</em><span>{row.family || 'Family unknown'}</span></button>{/each}
          {:else}<div class="species-result-message">No matching species</div>{/if}
        </div>
      {/if}
      <div class="highlight-footer">
        {#if $selectedSpecies}
          <div class="highlight-status" class:is-error={$speciesHighlight.status === 'error'} aria-live="polite"><span class="highlight-key" class:has-gradient={$speciesHighlightGradient} aria-hidden="true"></span><span>{#if $speciesHighlight.status === 'loading'}Loading distribution…{:else if $speciesHighlight.status === 'error'}{$speciesHighlight.message || 'Distribution unavailable'}{:else if $speciesHighlight.status === 'ready'}{$speciesHighlight.count.toLocaleString()} cells at resolution {$speciesHighlight.resolution}{/if}</span></div>
        {:else}<p class="species-hint">Matches show in blue.</p>{/if}
        <label class="gradient-toggle" title="Show the priority gradient inside highlighted cells"><span>Gradient</span><input type="checkbox" bind:checked={$speciesHighlightGradient} /><span class="gradient-switch" aria-hidden="true"><span></span></span></label>
      </div>
    </div>
  {:else}
    <button type="button" class="species-toggle" class:has-selection={Boolean($selectedSpecies)} aria-expanded={false} aria-controls="species-highlight-panel" title="Highlight a species" on:click={expand}>
      <Search size={17} strokeWidth={1.9} aria-hidden="true" />
    </button>
  {/if}
</section>

<svelte:window on:keydown={windowKeydown} />

<style>
  .species-highlight:not(.is-expanded) { width: auto; padding: 0; background: transparent; border: 0; box-shadow: none; backdrop-filter: none; }
  .species-toggle { position: relative; display: grid; place-items: center; width: 2.85rem; height: 2.85rem; padding: 0; color: var(--color-accent-brown); background: rgba(252, 251, 247, .96); border: 1px solid rgba(93, 78, 60, .2); border-radius: var(--map-radius, 4px); box-shadow: 0 9px 30px rgba(40, 32, 20, .16); cursor: pointer; backdrop-filter: blur(12px); transition: transform .16s ease, color .16s ease, border-color .16s ease; }
  .species-toggle:hover { color: var(--color-primary-green); border-color: rgba(93, 78, 60, .35); transform: translateY(-1px); }
  .species-toggle:focus-visible { outline: 2px solid var(--color-primary-green); outline-offset: 2px; }
  .species-toggle.has-selection::after { content: ''; position: absolute; top: -.1rem; right: -.1rem; width: .62rem; height: .62rem; background: var(--species-blue-cell-border); border-radius: 50%; box-shadow: 0 0 0 2px rgba(252, 251, 247, .96); }
  .species-panel { position: relative; text-align: left; animation: species-pop .18s cubic-bezier(.2, .9, .3, 1.2); transform-origin: top left; }
  .species-panel > label { display: block; margin: 0 0 .9rem .1rem; color: var(--color-accent-brown); font-family: var(--font-sans); font-size: .7rem; font-weight: 750; letter-spacing: .095em; text-align: left; text-transform: uppercase; }
  .species-hint { width: auto; text-align: left; }
  .species-close { position: absolute; z-index: 1; top: -.35rem; right: -.35rem; display: grid; place-items: center; width: 1.7rem; height: 1.7rem; padding: 0; color: var(--color-gray-600); background: var(--color-light-cream); border: 1px solid var(--color-tinted-cream); border-radius: 50%; cursor: pointer; box-shadow: 0 2px 8px rgba(40, 32, 20, .12); transition: color .15s ease, background .15s ease, transform .15s ease; }
  .species-close:hover { color: var(--color-black); background: var(--color-gray-100); transform: scale(1.06); }
  @keyframes species-pop {
    from { opacity: 0; transform: translateY(-.4rem) scale(.94); }
    to { opacity: 1; transform: none; }
  }
</style>
