# Ark-IV notices and data credits

This file records attribution for sources used to build Ark-IV. It does not
redistribute those sources and does not grant rights to their data. Consult the
linked provider terms before obtaining, processing, or publishing data.

## Restricted inputs

### IUCN Red List of Threatened Species

Ark-IV uses authorized IUCN Red List assessment and spatial data locally. IUCN
prohibits reposting or redistributing its raw/tabular/spatial data without
written permission. Its terms distinguish transformative derivative works,
which remain non-commercial and require acknowledgement, from insufficiently
transformative copies. Public browser delivery of Ark-IV's Arrow, PMTiles, or
API results therefore requires a separate written classification/permission
review. No IUCN rows, range geometry, or reconstructable database is intended
for this repository.

Citation for the current local build:

> IUCN. 2026. The IUCN Red List of Threatened Species. Version 2026-1.
> https://www.iucnredlist.org/. Accessed 26 August 2026.

- Terms: https://www.iucnredlist.org/terms/terms-of-use
- Citation guidance: https://nrl.iucnredlist.org/about/citationinfo
- Spatial downloads: https://www.iucnredlist.org/resources/spatial-data-download

### EDGE of Existence

EDGE lists are copyright Zoological Society of London (ZSL). ZSL's terms state
that site contents and data may not be reproduced, published, transmitted, or
redistributed without express written permission. Ark-IV therefore keeps EDGE
rows and rankings outside this code repository.

- Lists: https://www.edgeofexistence.org/download-edge-lists/
- Terms: https://www.edgeofexistence.org/terms-and-conditions/

### Protected Planet

Protected Planet WDPCA data is not bundled. Its current terms limit use to
non-commercial purposes and prohibit redistribution and sublicensing.

- Terms: https://www.protectedplanet.net/en/legal

## Product-specific licensed inputs

### HydroSHEDS HydroBASINS v1c

Ark-IV uses HydroBASINS standard level 1–12 polygons locally to resolve IUCN
`HYBAS_ID` relationship tables. HydroSHEDS states that product-specific terms
apply and that the HydroBASINS license is the agreement in its technical
documentation. A generated basin-cell index or basin-derived serving snapshot
must therefore be reviewed under that agreement rather than assumed to inherit
the application's AGPL licence.

- Product and citation guidance: https://www.hydrosheds.org/products/hydrobasins
- Current site terms: https://www.hydrosheds.org/terms-of-use
- v1 technical documentation and licence: https://data.hydrosheds.org/file/technical-documentation/HydroSHEDS_TechDoc_v1_4.pdf

## Open and redistributable sources

### GBIF Backbone Taxonomy — CC BY 4.0

> GBIF Secretariat. 2023. GBIF Backbone Taxonomy. Checklist dataset.
> https://doi.org/10.15468/39omei. Accessed 2026-08-26.

Dataset page: https://www.gbif.org/dataset/d7dddbf4-2cf0-4f39-9b2a-bb099caae36c

### Genomes on a Tree (GoaT)

Ark-IV uses GoaT/NCBI taxon identifiers and sequencing evidence. The public
`genomehubs/goat-data` repository is provided under the MIT License; any
third-party datasets it references retain their own terms.

- Service: https://goat.genomehubs.org/
- Data tooling and licence: https://github.com/genomehubs/goat-data

### Natural Earth — public domain

Countries and Admin-1 boundaries use Natural Earth 5.1.1. Natural Earth states
that its vector and raster data are in the public domain. Credit is optional;
Ark-IV uses the suggested wording: **Made with Natural Earth**.

- Terms: https://www.naturalearthdata.com/about/terms-of-use/

### geoBoundaries gbOpen

The global local-area layer uses all 180 available geoBoundaries gbOpen ADM2
country datasets pinned in `config/geoboundaries_adm2.toml`. A small fallback
preview retains Denmark, Germany and Sweden. Country source years, provider
URLs and upstream licences are preserved in the inventory and generated
country catalogues; the coverage report records source-count discrepancies.

> Runfola, D. et al. 2020. geoBoundaries: A global database of political
> administrative boundaries. PLOS ONE 15(4): e0231866.
> https://doi.org/10.1371/journal.pone.0231866

- API and licence: https://www.geoboundaries.org/api.html

### Marine Regions World EEZ v12 — CC BY 4.0

EEZ geometry is used locally to extend country scope into ocean waters. The
source archive and geometry are not mirrored by this repository.

> Flanders Marine Institute. 2023. Maritime Boundaries Geodatabase, version 12.
> https://doi.org/10.14284/628

- Source and citation: https://www.marineregions.org/sources.php

### RESOLVE Ecoregions 2017 — CC BY 4.0

> Dinerstein, E. et al. 2017. An Ecoregion-Based Approach to Protecting Half
> the Terrestrial Realm. BioScience 67(6), 534–545.
> https://doi.org/10.1093/biosci/bix014

- Data and licence: https://ecoregions.appspot.com/

### Basemap

The interactive map displays **© OpenStreetMap contributors © CARTO** in the
map attribution control. OpenStreetMap data is available under ODbL; CARTO's
Voyager basemap is used under CARTO's applicable terms.

- OpenStreetMap copyright: https://www.openstreetmap.org/copyright
- CARTO attribution: https://carto.com/attributions

## Software licence status

Copyright (C) 2026 Ark-IV contributors.

Ark-IV's application code is offered under the GNU Affero General Public
License v3.0 only (`AGPL-3.0-only`); see `LICENSE`. This grant covers project
code only. It does not relicense datasets, generated data artifacts, tutorial
media owned by third parties, or third-party software. Existing contributors'
agreement to apply AGPL to their contributions remains a publication gate.

Third-party software dependencies retain the licences declared by their
packages and lockfiles.

The direct Python and frontend dependencies declared on 26 August 2026 use
permissive MIT, Apache-2.0, BSD, or ISC licences. They are referenced through
the package manifests rather than vendored in this repository. A distributed
container, executable, or browser bundle must still retain the copyright,
licence, and NOTICE material required by every dependency actually included in
that artifact; regenerate and review a third-party notice manifest for each
binary deployment.
