<script lang="ts">
  import Sliders from '$lib/components/Sliders.svelte';
  import SpeciesTable from '$lib/components/SpeciesTable.svelte';
  import TableFilters from '$lib/components/TableFilters.svelte';
</script>

<svelte:head><title>Species table · Ark-IV</title></svelte:head>
<main class="main-content">
  <h1>Exploring The Raw Data</h1>
  <p class="page-description">Explore sequencing status by species or family with typo-tolerant text search, sorting, spatial filters, and custom scoring.</p>
  <Sliders showSystems={false} />
  <TableFilters />
  <SpeciesTable />
</main>
