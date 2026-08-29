<svelte:head>
  <title>About the data · Ark-IV</title>
  <meta name="description" content="How Ark-IV combines conservation status, taxonomy, DNA evidence, and species distributions." />
</svelte:head>

<main class="main-content">
  <div class="tutorial-container about-data-page">
    <header class="tutorial-header">
      <p class="about-kicker">Methods &amp; provenance</p>
      <h1>About the data</h1>
      <p class="tutorial-intro">Ark-<i>IV</i> connects extinction risk with gaps in DNA sequencing evidence. This page explains where each signal comes from, how global records are joined, and what the scores mean.</p>
    </header>

    <section class="tutorial-section"><div class="tutorial-subsection layout-single-column is-visible">
      <div class="about-content"><h2>Five sources, five distinct roles</h2><p>No single dataset supplies a complete conservation priority. Ark-IV preserves the role and identifier provenance of each source as they are combined.</p></div>
      <ul class="source-ledger">
        <li><span class="source-index">01</span><div><strong><a href="https://www.iucnredlist.org/search" target="_blank" rel="noreferrer">IUCN Red List</a></strong><p>Stable SIS taxon and assessment IDs, scientific names, extinction-risk categories, habitat systems, and global spatial ranges.</p></div></li>
        <li><span class="source-index">02</span><div><strong><a href="https://www.gbif.org/dataset/d7dddbf4-2cf0-4f39-9b2a-bb099caae36c" target="_blank" rel="noreferrer">GBIF Backbone Taxonomy</a></strong><p>Accepted taxon identifiers for unambiguous links to GBIF records; exact unique matches are retained separately from the application key.</p></div></li>
        <li><span class="source-index">03</span><div><strong><a href="https://goat.genomehubs.org/" target="_blank" rel="noreferrer">Genomes on a Tree (GoaT)</a></strong><p>NCBI taxon identifiers plus sequencing, sample, assembly, and broader Tree of Life evidence at species, genus, and family level.</p></div></li>
        <li><span class="source-index">04</span><div><strong><a href="https://www.edgeofexistence.org/download-edge-lists/" target="_blank" rel="noreferrer">EDGE of Existence</a></strong><p>Global evolutionary-distinctiveness lists joined to species by IUCN Red List ID.</p></div></li>
        <li><span class="source-index">05</span><div><strong>Global boundary frameworks</strong><p>Natural Earth countries and states, geoBoundaries administrative areas, and RESOLVE terrestrial ecoregions used for global spatial filtering.</p></div></li>
      </ul>
    </div><div class="section-divider"><span>#</span></div></section>

    <section class="tutorial-section"><div class="tutorial-subsection layout-single-column is-visible">
      <div class="about-content"><h2>How a species record moves through Ark-IV</h2><p>The IUCN SIS taxon ID remains the stable application key. Exact source identifiers are attached without treating different taxonomic concepts as interchangeable.</p></div>
      <ol class="data-flow">
        <li><span>1</span><div><strong>Start with IUCN</strong><p>Retain each assessed taxon, its assessment, Red List category, systems, and range.</p></div></li>
        <li><span>2</span><div><strong>Attach exact source IDs</strong><p>Add a unique accepted GBIF taxon ID and reviewed GoaT/NCBI taxon ID when the global crosswalk supports them.</p></div></li>
        <li><span>3</span><div><strong>Evaluate GoaT evidence</strong><p>Evaluate samples and assemblies when a populated match exists; otherwise retain the taxon as GoaT Data Deficient.</p></div></li>
        <li><span>4</span><div><strong>Attach geography</strong><p>Associate the species with global H3 cells and its terrestrial, freshwater, or marine systems.</p></div></li>
      </ol>
    </div><div class="section-divider"><span>#</span></div></section>

    <section class="tutorial-section"><div class="tutorial-subsection layout-single-column is-visible">
      <div class="about-content"><h2>What counts as species-level DNA evidence?</h2><p>A species is treated as sampled when the first applicable evidence rule below succeeds. A false result means that the available GoaT record does not satisfy any rule—not necessarily that sequencing can never have occurred elsewhere.</p></div>
      <div class="tutorial-table-wrapper about-table-wrapper"><table class="tutorial-table about-table"><thead><tr><th>Order</th><th>Evidence rule</th><th>DNA evidence</th></tr></thead><tbody>
        <tr><td>1</td><td>IUCN category is Extinct or Extinct in the Wild</td><td><strong>Present</strong></td></tr>
        <tr><td>2</td><td>GoaT reports a sample acquired or sequencing in progress</td><td><strong>Present</strong></td></tr>
        <tr><td>3</td><td>GoaT reports EBP-standard criteria</td><td><strong>Present</strong></td></tr>
        <tr><td>4</td><td>Chromosome or complete-genome assembly with BUSCO completeness ≥ 90%</td><td><strong>Present</strong></td></tr>
        <tr><td>5</td><td>No rule above is satisfied</td><td><strong>Missing</strong></td></tr>
      </tbody></table></div>
    </div><div class="section-divider"><span>#</span></div></section>

    <section class="tutorial-section"><div class="tutorial-subsection layout-single-column is-visible">
      <div class="about-content"><h2>How the DNA gap is selected</h2><p>Each species receives one mutually exclusive DNA-gap category. The first matching condition wins, so the table and map apply exactly one DNA weight per species.</p></div>
      <div class="tutorial-table-wrapper about-table-wrapper"><table class="tutorial-table about-table"><thead><tr><th>Order</th><th>Condition</th><th>DNA gap</th><th>Default</th></tr></thead><tbody>
        <tr><td>1</td><td>No populated GoaT taxon match</td><td><span class="method-pill method-gdd">GoaT Data Deficient</span></td><td>4.0</td></tr>
        <tr><td>2</td><td>No DNA evidence in the family</td><td><span class="method-pill method-family">Missing Family</span></td><td>4.0</td></tr>
        <tr><td>3</td><td>Family covered, but no evidence in the genus</td><td><span class="method-pill method-genus">Missing Genus</span></td><td>3.0</td></tr>
        <tr><td>4</td><td>Genus covered, but no evidence for the species</td><td><span class="method-pill method-species">Missing Species</span></td><td>2.0</td></tr>
        <tr><td>5</td><td>Species-level evidence is present</td><td><span class="method-pill method-sampled">Already Sampled</span></td><td>0.0</td></tr>
      </tbody></table></div>
      <aside class="deficiency-note" aria-labelledby="two-deficiencies"><h3 id="two-deficiencies">Two kinds of “data deficient”</h3><div class="definition-grid"><div><span class="definition-source">IUCN</span><strong>Data Deficient</strong><p>There is not enough conservation information to assess extinction risk. This affects the threat side of the score.</p></div><div><span class="definition-source">GoaT</span><strong>Data Deficient</strong><p>Ark-IV could not find a populated GoaT taxon match. This affects the DNA-gap side and is not a claim that DNA is confirmed absent.</p></div></div></aside>
    </div><div class="section-divider"><span>#</span></div></section>

    <section class="tutorial-section"><div class="tutorial-subsection layout-single-column is-visible">
      <div class="about-content"><h2>How priority is calculated</h2><p class="formula-line"><span>Priority</span><b>=</b><span>IUCN weight</span><b>×</b><span>DNA-gap weight</span></p><p>Weights are adjustable in the table and map. Defaults provide a useful starting point rather than a universal statement of conservation value.</p></div>
      <div class="score-table-grid"><div class="tutorial-table-wrapper about-table-wrapper"><table class="tutorial-table about-table"><thead><tr><th>IUCN category</th><th>Default</th></tr></thead><tbody><tr><td>Critically Endangered</td><td>4.0</td></tr><tr><td>Endangered</td><td>3.0</td></tr><tr><td>Vulnerable</td><td>2.0</td></tr><tr><td>Near Threatened</td><td>1.0</td></tr><tr><td>Data Deficient</td><td>2.0</td></tr><tr><td>Least Concern</td><td>0.1</td></tr></tbody></table></div><div class="tutorial-table-wrapper about-table-wrapper"><table class="tutorial-table about-table"><thead><tr><th>DNA gap</th><th>Default</th></tr></thead><tbody><tr><td>GoaT Data Deficient</td><td>4.0</td></tr><tr><td>Missing Family</td><td>4.0</td></tr><tr><td>Missing Genus</td><td>3.0</td></tr><tr><td>Missing Species</td><td>2.0</td></tr><tr><td>Already Sampled</td><td>0.0</td></tr></tbody></table></div></div>
    </div><div class="section-divider"><span>#</span></div></section>

    <section class="tutorial-section"><div class="tutorial-subsection layout-single-column is-visible">
      <div class="about-content"><h2>Licences and credits</h2><p>Ark-IV application code is available under <a href="https://github.com/karlaugustnybo/DIKUs-Ark/blob/main/LICENSE" target="_blank" rel="noreferrer">AGPL-3.0-only</a>. That code licence does not grant permission to redistribute a dataset. The public code release excludes IUCN and EDGE rows, global distributions, and generated serving databases.</p></div>
      <ul class="source-ledger">
        <li><span class="source-index">IUCN</span><div><strong>IUCN 2026, Red List version 2026-1</strong><p>Used under the <a href="https://www.iucnredlist.org/terms/terms-of-use" target="_blank" rel="noreferrer">IUCN Red List Terms of Use</a>; raw, tabular, and spatial data are not bundled. Browser-delivered derivatives and APIs require a separate publication review.</p></div></li>
        <li><span class="source-index">GBIF</span><div><strong><a href="https://doi.org/10.15468/39omei" target="_blank" rel="noreferrer">GBIF Backbone Taxonomy (2023)</a></strong><p>CC BY 4.0; accessed 26 August 2026.</p></div></li>
        <li><span class="source-index">EDGE</span><div><strong><a href="https://www.edgeofexistence.org/terms-and-conditions/" target="_blank" rel="noreferrer">ZSL EDGE of Existence</a></strong><p>EDGE rows and ranks are kept outside the code release unless express redistribution permission is obtained.</p></div></li>
        <li><span class="source-index">MAP</span><div><strong>Boundary and basemap sources</strong><p><a href="https://www.naturalearthdata.com/about/terms-of-use/" target="_blank" rel="noreferrer">Natural Earth</a> (public domain), <a href="https://www.geoboundaries.org/api.html" target="_blank" rel="noreferrer">geoBoundaries gbOpen</a> and <a href="https://ecoregions.appspot.com/" target="_blank" rel="noreferrer">RESOLVE Ecoregions 2017</a> (CC BY 4.0), and <a href="https://www.marineregions.org/sources.php" target="_blank" rel="noreferrer">Marine Regions World EEZ v12</a> (CC BY 4.0). The basemap retains © OpenStreetMap contributors © CARTO in-map attribution.</p></div></li>
      </ul>
    </div><div class="section-divider"><span>#</span></div></section>

    <section class="tutorial-section layout-single-column about-notes"><div class="about-content"><h2>Interpretation notes</h2></div><dl class="method-notes">
      <div><dt>Taxonomy</dt><dd>IUCN taxon IDs stay primary. GoaT/NCBI and GBIF identifiers remain source-specific links, and unresolved matches remain visibly absent.</dd></div>
      <div><dt>Taxon identity</dt><dd>Ark-IV never deduplicates distinct IUCN taxa by a shared NCBI or GBIF concept; this avoids silently collapsing splits and lumps.</dd></div>
      <div><dt>Search</dt><dd>Species and family matching treats input as ordinary text, ignores case and accents, ranks exact and prefix matches first, and tolerates minor misspellings.</dd></div>
      <div><dt>System classification</dt><dd>IUCN classifies species as terrestrial, freshwater, marine, or a combination. Every system is aggregated independently on the map.</dd></div>
      <div><dt>Access and serving</dt><dd>PostgreSQL serves search, compact coarse species distributions, and selected-cell details. PMTiles serves global context; fine global cells are read from spatially partitioned Parquet on demand.</dd></div>
      <div><dt>Terms and citation</dt><dd>Source links do not replace dataset-specific terms. IUCN data use is restricted to permitted non-commercial conservation, education, and research use; downstream releases must retain current source citations and licensing notices.</dd></div>
    </dl></section>
  </div>
</main>
