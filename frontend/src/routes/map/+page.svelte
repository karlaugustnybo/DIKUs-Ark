<script lang="ts">
  import Sliders from '$lib/components/Sliders.svelte';
  import Map from '$lib/components/Map.svelte';
  import CellDetails from '$lib/components/CellDetails.svelte';
</script>

<svelte:head><title>Explore the map · Ark-IV</title></svelte:head>
<main class="main-content">
  <h1>Explore The Map</h1>
  <p class="page-description">Explore worldwide DNA-sequencing priorities across terrestrial, freshwater, and marine ecosystems, or create your own scoring function.</p>
  <Sliders />
  <Map />
  <CellDetails />
</main>
