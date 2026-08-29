<script lang="ts">
  import { onMount } from 'svelte';
  import '../app.css';
  import { preloadMapBundle, preloadMapBundleInIdleTime } from '$lib/map/mapBundle';
  import { navigateToPreloadedMap } from '$lib/map/mapNavigation';
  let { children } = $props();
  const warmMap = () => { void preloadMapBundle().catch(() => {}); };

  // Warm the global map after the current page becomes idle. Direct map intent
  // (hover, focus, or pointer-down) starts it immediately, while slow/save-data
  // connections are left alone until the user actually opens the map.
  onMount(preloadMapBundleInIdleTime);
</script>

<svelte:head>
  <title>Ark-IV</title>
  <meta name="description" content="Worldwide conservation DNA sequencing priorities" />
  <link rel="icon" href="/favicon.svg" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin="anonymous" />
  <link href="https://fonts.googleapis.com/css2?family=Baskervville:ital@0;1&family=JetBrains+Mono:ital,wght@0,100..800;1,100..800&family=Noto+Serif+Display:ital,wght@0,100..900;1,100..900&family=Noto+Sans:ital,wght@0,100..900;1,100..900&display=swap" rel="stylesheet" />
</svelte:head>

<nav class="site-navbar">
  <div class="nav-left"><a class="nav-brand" href="/">Ark-<i>IV</i></a></div>
  <div class="nav-right" role="navigation" aria-label="Main">
    <a href="/table"><span>Table</span></a><a href="/map" onclick={navigateToPreloadedMap} onpointerenter={warmMap} onfocus={warmMap} onpointerdown={warmMap}><span>Map</span></a><a href="/tutorial"><span>Tutorial</span></a><a href="/about-data"><span>About the Data</span></a>
  </div>
</nav>
{@render children()}
<footer class="site-footer"><p>Ark-<i>IV</i> · Conservation prioritisation powered by IUCN and GoaT data · <a href="https://github.com/karlaugustnybo/DIKUs-Ark" target="_blank" rel="noreferrer">Source (AGPL-3.0-only)</a></p></footer>
