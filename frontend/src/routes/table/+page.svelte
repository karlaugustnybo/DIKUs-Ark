<script lang="ts">
  import Sliders from '$lib/components/Sliders.svelte';
  import SpeciesTable from '$lib/components/SpeciesTable.svelte';
</script>

<svelte:head><title>Species table · Ark-IV</title></svelte:head>
<main class="main-content">
  <h1>Exploring The Raw Data</h1>
  <p class="page-description">Scroll through real sequencing-status and build a more detailed view of each datapoint, use REGEX search or modify the scoring function.</p>
  <Sliders showSystems={false} />
  <SpeciesTable />
</main>
