<script lang="ts">
  import '../app.css';
  let { children } = $props();
</script>

<svelte:head>
  <title>Ark-IV</title>
  <meta name="description" content="Conservation DNA sequencing priorities for Denmark" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="anonymous" />
  <link href="https://fonts.googleapis.com/css2?family=Baskervville:ital@0;1&family=JetBrains+Mono:ital,wght@0,100..800;1,100..800&family=Noto+Serif+Display:ital,wght@0,100..900;1,100..900&family=Noto+Sans:ital,wght@0,100..900;1,100..900&display=swap" rel="stylesheet" />
</svelte:head>

<nav class="site-navbar">
  <div class="nav-left"><a class="nav-brand" href="/">Ark-<i>IV</i></a></div>
  <div class="nav-right" role="navigation" aria-label="Main">
    <a href="/table"><span>Table</span></a><a href="/map"><span>Map</span></a><a href="/tutorial"><span>Tutorial</span></a><a href="/about-data"><span>About the Data</span></a>
  </div>
</nav>
{@render children()}
<footer class="site-footer"><p>Ark-<i>IV</i> a Conservation prioritisation tool powered by IUCN and GoaT data.</p></footer>
