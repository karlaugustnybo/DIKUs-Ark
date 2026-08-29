<script lang="ts">
  import { ExternalLink } from 'lucide-svelte';

  export let href: string;
  export let source: 'IUCN' | 'GBIF' | 'GoaT';
  export let context: string;
  export let fused = false;
  export let className = '';
</script>

<a
  class={`source-link source-${source.toLowerCase()}${fused ? ' source-link-fused' : ''}${className ? ` ${className}` : ''}`}
  {href}
  target="_blank"
  rel="noopener noreferrer"
  aria-label={`View ${context} on ${source} (opens in a new tab)`}
  title={`View on ${source}`}
>
  <slot>{source}</slot><ExternalLink size={11} strokeWidth={2} aria-hidden="true" />
</a>
