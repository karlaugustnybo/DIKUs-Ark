<svelte:head><title>About the data · Ark-IV</title></svelte:head>

<main class="main-content">
  <div class="tutorial-container">
    <header class="tutorial-header"><h1>About the data</h1><p class="tutorial-intro">Learn about the origins of the data behind Ark-<i>IV</i>.</p></header>

    <section class="tutorial-section">
      <div class="tutorial-subsection layout-single-column is-visible">
        <div class="about-content"><h2>The data behind the website</h2><p>We used many different datasets in the creation of this web app:</p></div>
        <div class="about-content"><ul class="about-points">
          <li><strong><a href="https://goat.genomehubs.org/" target="_blank" rel="noreferrer">Genomes on a Tree (GoaT)</a></strong><p>Supplies comprehensive metrics regarding DNA sequencing status across individual species and the broader Tree of Life.</p></li>
          <li><strong><a href="https://www.iucnredlist.org/search" target="_blank" rel="noreferrer">International Union for Conservation of Nature (IUCN)</a></strong><p>Yields categorical extinction risks from the Red List of Threatened Species and high-resolution spatial data layers.</p></li>
          <li><strong><a href="https://www.gbif.org/dataset/d7dddbf4-2cf0-4f39-9b2a-bb099caae36c" target="_blank" rel="noreferrer">Global Biodiversity Information Facility (GBIF)</a></strong><p>Provides the backbone taxonomy used to join disparate records under verified taxonomic keys.</p></li>
          <li><strong><a href="https://www.edgeofexistence.org/download-edge-lists/" target="_blank" rel="noreferrer">EDGE of Existence</a></strong><p>Adds evolutionary distinctiveness metrics combined with global threat assessments.</p></li>
          <li><strong><a href="https://ec.europa.eu/eurostat/web/gisco/geodata/administrative-units/countries" target="_blank" rel="noreferrer">GISCO (Eurostat)</a></strong><p>Provides geospatial boundaries for Denmark's territorial landmass and marine waters.</p></li>
        </ul></div>
      </div>
      <div class="section-divider"><span>#</span></div>
    </section>

    <section class="tutorial-section">
      <div class="tutorial-subsection layout-single-column is-visible">
        <div class="about-content"><h2>How species DNA status was calculated</h2></div>
        <div class="tutorial-table-wrapper"><table class="tutorial-table"><thead><tr><th>Order</th><th>Condition</th><th>Result</th></tr></thead><tbody>
          <tr><td>1</td><td>redlist_category IN ('EX', 'EW')</td><td><strong>true</strong></td></tr>
          <tr><td>2</td><td>sample_acquired IS NOT NULL OR in_progress IS NOT NULL</td><td><strong>true</strong></td></tr>
          <tr><td>3</td><td>ebp_standard_criteria IS NOT NULL</td><td><strong>true</strong></td></tr>
          <tr><td>4</td><td>assembly_level IN ('Chromosome', 'Complete Genome') AND busco_completeness &gt;= 90.0</td><td><strong>true</strong></td></tr>
          <tr><td>5</td><td>Default fallback</td><td><strong>false</strong></td></tr>
        </tbody></table></div>
        <div class="about-content"><p>GoaT takes precedence over IUCN for genus and family names so they match the Tree of Life. Scientific species names are from IUCN.</p></div>
      </div>
      <div class="section-divider"><span>#</span></div>
    </section>

    <section class="tutorial-section">
      <div class="tutorial-subsection layout-single-column is-visible">
        <div class="about-content"><h2>How the Priority Score is calculated</h2><p>Priority = Threat Score × DNA Level Score. The threat score comes from the species' IUCN category; the DNA score uses the highest missing DNA level.</p></div>
        <div class="tutorial-table-wrapper"><table class="tutorial-table"><thead><tr><th>IUCN</th><th>Default</th></tr></thead><tbody>
          <tr><td>Critically Endangered</td><td>4.0</td></tr><tr><td>Endangered</td><td>3.0</td></tr><tr><td>Vulnerable</td><td>2.0</td></tr><tr><td>Near Threatened</td><td>1.0</td></tr><tr><td>Data Deficient</td><td>2.0</td></tr><tr><td>Least Concern</td><td>0.1</td></tr>
        </tbody></table></div>
        <div class="tutorial-table-wrapper"><table class="tutorial-table"><thead><tr><th>GoaT</th><th>Default</th></tr></thead><tbody>
          <tr><td>Missing Species DNA</td><td>2.0</td></tr><tr><td>Missing Genus DNA</td><td>3.0</td></tr><tr><td>Missing Family DNA</td><td>4.0</td></tr><tr><td>Already Sampled</td><td>0.0</td></tr>
        </tbody></table></div>
      </div>
      <div class="section-divider"><span>#</span></div>
    </section>

    <section class="tutorial-section layout-single-column">
      <div class="about-content"><h1>Other remarks</h1></div>
      <div class="about-content"><h2>Deduplication</h2><p>Species with the same GBIF ID can have several IUCN rows. Duplicates are removed and the most conservative category is retained.</p></div>
      <div class="about-content"><h2>Search</h2><p>The species table searches both <strong>species_name</strong> and <strong>family</strong>.</p></div>
      <div class="about-content"><h2>System classification</h2><p>IUCN system data classifies species as terrestrial, freshwater, marine, or a combination. Terrestrial and freshwater cells are rendered over land and marine cells over sea, and every system is scored independently.</p></div>
      <div class="about-content"><h2>Serving architecture</h2><p>DuckDB and Tippecanoe prebuild raw cell aggregates into PMTiles, so moving the map never queries the database. PostgreSQL serves searches and selected-cell details through Litestar. Bulk data remains available as <a href="/exports/species.parquet">species Parquet</a> and <a href="/exports/cell_species.parquet">cell-species Parquet</a>.</p></div>
    </section>
  </div>
</main>
