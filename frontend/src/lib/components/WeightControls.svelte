<script lang="ts">
  import { weights, type WeightKey } from '$lib/stores/weights';

  export let title: string;
  export let controls: Array<{ key: WeightKey; label: string; step?: number }>;
  export let open = false;

  function setWeight(key: WeightKey, value: string) {
    const parsed = Math.min(10, Math.max(0, Number(value)));
    if (Number.isFinite(parsed)) weights.update((current) => ({ ...current, [key]: parsed }));
  }
</script>

<div class="sliders-dropdown">
  <button type="button" class:is-open={open} class="dropdown-toggle" on:click={() => (open = !open)} aria-expanded={open}>
    {title}
    <svg class="chevron-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
      <path d="m19.5 8.25-7.5 7.5-7.5-7.5" />
    </svg>
  </button>
  <div class:js-hidden={!open} class="dropdown-panel">
    <div class="dropdown-grid">
      {#each controls as control}
        <div class="weight-row">
          <label for={control.key}>{control.label}</label>
          <div class="weight-controls">
            <input id={control.key} type="range" min="0" max="10" step={control.step ?? 0.5} value={$weights[control.key]} on:input={(event) => setWeight(control.key, event.currentTarget.value)} />
            <input aria-label={`${control.label} value`} class="weight-num" type="number" min="0" max="10" step={control.step ?? 0.5} value={$weights[control.key]} on:input={(event) => setWeight(control.key, event.currentTarget.value)} />
          </div>
        </div>
      {/each}
    </div>
  </div>
</div>
