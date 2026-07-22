<script lang="ts">
  import Sliders from '$lib/components/Sliders.svelte';
  import Map from '$lib/components/Map.svelte';
  import CellDetails from '$lib/components/CellDetails.svelte';
</script>

<svelte:head><title>Explore the map · Ark-IV</title></svelte:head>
<main class="main-content">
  <h1>Explore The Map</h1>
  <p class="page-description">See where in the world there is the highest need for DNA-sequencing, discover target-areas local to yourself, or make your own custom scoring function.</p>
  <Sliders />
  <Map />
  <CellDetails />
</main>
