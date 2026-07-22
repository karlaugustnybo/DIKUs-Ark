<svelte:head><title>Tutorial · Ark-IV</title></svelte:head>

<main class="main-content">
  <div class="tutorial-container">
    <header class="tutorial-header"><h1>Tutorial</h1><p class="tutorial-intro">Learn how to use the table and map to explore species prioritization data.</p></header>

    <section class="tutorial-section">
      <h2>The Table</h2><p class="section-description">Explore species data row-by-row with search, sorting, pagination, and custom scoring.</p>
      <div class="tutorial-subsection layout-side-by-side is-visible"><div class="tutorial-content"><h3>Searching &amp; Filtering</h3><p>Use the search box to filter by species or family and quickly zero in on the records you need.</p></div><figure class="tutorial-figure"><video class="feature-gif" autoplay loop muted playsinline><source src="/imgs/feat_search.mov" /></video><figcaption>Type a species or family name to narrow results.</figcaption></figure></div>
      <div class="tutorial-subsection layout-single-column is-visible"><div class="tutorial-content"><h3>Sorting Columns</h3></div><figure class="tutorial-figure"><video class="feature-gif" autoplay loop muted playsinline><source src="/imgs/feat_sorting_2.mov" /></video><figcaption>Sort by threat level, DNA status, or priority score.</figcaption></figure><div class="tutorial-content"><p>Click any column heading to change the sort field. Click it again to reverse the order.</p></div></div>
      <div class="tutorial-subsection layout-split-header is-visible"><h3>Adjusting Scoring Weights</h3><div class="split-body"><div class="tutorial-content"><p>Open the sliders and customise how each IUCN threat level and DNA sampling status contributes to the priority score. Results update immediately.</p></div><figure class="tutorial-figure"><video class="feature-gif" autoplay loop muted playsinline><source src="/imgs/feat_sliders.mov" /></video><figcaption>Change slider weights and see priorities recalculate.</figcaption></figure></div></div>
      <div class="tutorial-subsection layout-single-column is-visible"><div class="tutorial-content"><h3>Pagination</h3></div><figure class="tutorial-figure"><video class="feature-gif" autoplay loop muted playsinline><source src="/imgs/feat_pagination.mov" /></video><figcaption>Use the buttons to move between pages.</figcaption></figure></div>
      <div class="section-divider"><span>#</span></div>
    </section>

    <section class="tutorial-section">
      <h2>The Map</h2><p class="section-description">Visualize species distribution and priority across Denmark's hexagonal grid.</p>
      <div class="tutorial-subsection layout-side-by-side is-visible"><div class="tutorial-content"><h3>Zooming &amp; Panning</h3><p>Drag to move across Denmark and use the scroll wheel or touchpad to zoom. The map switches from coarse H3 resolution 3 to detailed resolution 7 beyond zoom level 7.</p></div><figure class="tutorial-figure"><video class="feature-gif" autoplay loop muted playsinline><source src="/imgs/feat_movement.mov" /></video><figcaption>Zoom smoothly switches between coarse and fine hexagons.</figcaption></figure></div>
      <div class="tutorial-subsection layout-split-header is-visible"><h3>Color Intensity</h3><div class="split-body"><div class="tutorial-content"><p>Each cell's color represents its priority score. Purple, blue, and green indicate lower priority; yellow, orange, and red indicate higher priority.</p></div><figure class="tutorial-figure"><video class="feature-gif" autoplay loop muted playsinline><source src="/imgs/feat_heat.mov" /></video><figcaption>Warmer colors show higher-priority regions.</figcaption></figure></div></div>
      <div class="tutorial-subsection layout-single-column is-visible"><div class="tutorial-content"><h3>Hovering &amp; Details</h3></div><figure class="tutorial-figure"><video class="feature-gif" autoplay loop muted playsinline><source src="/imgs/feat_onehot.mov" /></video><figcaption>Hover for aggregate values and click for species details.</figcaption></figure><div class="tutorial-content"><p>Hover over a hexagon to see its score. Clicking it opens conservation and DNA statistics plus the full species list for that cell.</p></div></div>
      <div class="tutorial-subsection layout-side-by-side layout-side-by-side--reverse is-visible"><div class="tutorial-content"><h3>Adjusting Weights in Real-Time</h3><p>Move any slider while viewing the map. The raw counts already in PMTiles are rescored in the browser, so every visible cell recolors without an API request.</p></div><figure class="tutorial-figure"><video class="feature-gif" autoplay loop muted playsinline><source src="/imgs/feat_heatsliders.mov" /></video><figcaption>Move sliders to recalculate map priorities.</figcaption></figure></div>
      <div class="tutorial-subsection layout-single-column is-visible"><div class="tutorial-content"><h3>Extra Functionality</h3></div><figure class="tutorial-figure"><video class="feature-gif" autoplay loop muted playsinline><source src="/imgs/feat_fullscreen.mov" /></video><figcaption>Use the full map area for detailed exploration.</figcaption></figure></div>
    </section>

    <div class="tutorial-subsection layout-single-column is-visible"><h3>Map View Shortcuts</h3><div class="tutorial-table-wrapper"><table class="tutorial-table"><thead><tr><th>Input</th><th>Action</th></tr></thead><tbody>
      <tr><td><strong>Mouse drag</strong></td><td>Pan the map</td></tr><tr><td><strong>Mouse scroll / pinch</strong></td><td>Zoom in or out</td></tr><tr><td><strong>Click cell</strong></td><td>Open its species details</td></tr><tr><td><strong>System buttons</strong></td><td>Switch between all, terrestrial, freshwater, and marine layers</td></tr>
    </tbody></table></div></div>

    <footer class="tutorial-footer"><p><strong>Ready to explore?</strong> Start with the <a href="/table">table</a> or jump to the <a href="/map">map</a>.</p></footer>
  </div>
</main>
