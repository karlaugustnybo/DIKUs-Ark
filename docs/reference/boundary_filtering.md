# Boundary filtering

Ark-IV separates boundary definitions from H3 biodiversity metrics. A small,
geometry-free catalogue populates each selector; boundary polygons are used by
the build and dynamic-tile services but are never rendered as map overlays.
Cell memberships remain compact delimiter-encoded sets, so
filtering does not require downloading or intersecting boundary geometry in the
browser.

## Bundled frameworks

| Framework | Relationship | H3 tile property | Source |
| --- | --- | --- | --- |
| Admin-0 countries and territories | Every intersected polygon | `j` | Natural Earth 10m Admin-0 |
| Admin-1 states and provinces | Every intersected polygon | `a1` | Natural Earth 10m Admin-1 |
| Municipalities and local areas | Every intersected polygon | `mun` | geoBoundaries gbOpen ADM2, all 180 available countries |
| Exclusive economic zones | Every intersected polygon | `eez` | Marine Regions World EEZ v12 |
| Terrestrial ecoregions | Every intersected polygon | `eco` | RESOLVE Ecoregions 2017 |

Natural Earth is public-domain data. geoBoundaries gbOpen, Marine Regions World
EEZ v12, and RESOLVE Ecoregions 2017 carry attribution requirements. The installed
global ADM2 layer contains **49,308 areas in 180 countries**; the small checked-in
fallback preview contains 426 units across Denmark, Germany, and Sweden.
geoBoundaries' upstream source licences are retained in each country catalogue
and the coverage report. ADM2 means the second administrative level, which can
be a municipality, county, district or another local area depending on the country.
The ecoregion catalogue contains
all 847 terrestrial ecoregions, including biome, realm, and conservation-status
metadata.

The EEZ source snapshot stays in ignored `data/sources/` because Marine Regions
asks users to obtain its products from the official service rather than a mirror.
The normalized local build assigns each EEZ to every territory and sovereign
ISO-3 code supplied by Marine Regions. Country scope is the union of Admin-0
land and those matching EEZ polygons. EEZ remains an internal membership and
location-context field, but it is not shown as a separate filter because that
would duplicate part of the country scope.

Protected Planet remains source-required. Since November 2025 its WDPA and
WD-OECM records use the merged WDPCA model. Non-commercial users can accept the
download terms on Protected Planet, or request a token for the v4 API at
<https://api.protectedplanet.net/documentation>. Do not commit the source or
derived protected-area geometries.

## Loading and geometry strategy

The Admin-1 selector previously downloaded the entire 5.6 MB GeoJSON before it
could show a name. It now asks for a country first and loads that country's
catalogue—typically 1–20 KB—instead of the roughly 0.75 MB worldwide catalogue.
An explicit “browse all” action remains available. Municipality catalogues use
the same parent-scoped strategy. Selecting a boundary changes only which H3
cells are visible; it does not trigger a browser geometry request.

Catalogue requests are cached for the life of the page. The result
list initially mounts 72 rows and adds more in bounded batches, while search
still considers every loaded record. This makes reopening a tab effectively
immediate and avoids both re-parsing polygons and constructing thousands of
checkbox elements merely to search names.

`just data-boundaries` acquires and installs the pinned global ADM2 snapshot.
It also runs before global map preparation, including `just data-prepare --tiles`.
The download is registered under `geoboundaries-adm2` in the ordinary acquisition
manifest. Individual countries are cached during interrupted downloads and
unchanged country files can be reused from a previous snapshot.

The installed geometry lives at `data/boundaries/adm2/current/municipality.geojson`.
Both backend settings and the managed PMTiles exporter select it automatically;
an explicit `MUNICIPALITY_BOUNDARIES_PATH` still takes precedence. Code-only
checkouts retain the bundled preview. The ignored `app/static/data/adm2-catalogs`
symlink exposes only country-sized, geometry-free catalogues to the frontend.
Its descriptor replaces the preview's coverage information. Countries absent
from the source have empty catalogues and an explicit unavailable-coverage label.
Source codes PSE, SSD and XKX map to the existing country selector's PSX, SDS and
KOS codes. The coverage report retains source country codes and ADM2 identifiers
remain unchanged; geometry and catalogue parent codes match the country selector.
The worldwide geometry is never copied into the frontend asset directory.

Inputs are pinned in `config/geoboundaries_adm2.toml`, including per-country
source commit URLs, represented years, upstream licences and reported counts.
Provider-simplified geometry is used for map filtering. When its count disagrees
with the API, the downloader checks the exact ID set against the pinned full
source. The verified total is 49,308, versus the API's reported 49,363. Slovenia
needed one area restored from its full file; Suriname uses its full dataset
because the simplified file has inconsistent identifiers. Every such decision
is recorded in `coverage.json` and the source metadata. These are source-version
boundaries, not a claim that every administrative change through 2026 is covered.

## Filter semantics

- Multiple selections in one framework are a union: Denmark **or** Italy.
- Selections in different frameworks are an intersection: Denmark **and**
  Midtjylland.
- A cell crossing a border belongs to every boundary its hexagon intersects,
  including a boundary touched only at an edge or corner.
- Cells without a matching polygon have an empty membership set.
- The species table uses resolution-3 cell memberships and compact species-ID
  arrays to find species occurring within the selected spatial scope.
- A selected framework uses its local score domain. Selections within one
  framework union their domains; intersected frameworks use the tightest
  applicable maximum. Missing/stale domain metadata fails visibly rather than
  silently falling back to the global colour scale.

## Generated and serving artifacts

- `app/static/data/boundary-frameworks.json` describes available and planned
  framework adapters.
- `app/static/data/boundaries/*.geojson` contains build/runtime intersection
  geometry for the bundled frameworks and the municipality fallback preview.
- `data/boundaries/adm2/current/` contains the installed global municipality
  geometry, country catalogues, source coverage report and checksum receipt.
- `data/boundaries/eez.geojson` and `country-scope.geojson` are ignored local
  build/runtime geometry so the Marine Regions product is not republished.
- `app/static/data/boundary-catalogs/*.json` contains selector records without
  polygon coordinates.
- `app/static/data/boundary-geometry/` contains optional rebuildable source
  partitions. It is ignored by Git and the browser does not request or render it.
- PMTiles features carry `j`, `a1`, `mun`, `eez`, and `eco` properties. Multiple codes
  use a compact `|` delimiter because vector-tile attributes are scalar values.
- `data/exports/cell_boundaries.parquet` carries resolution-3 memberships into
  PostgreSQL array columns in the `cell_boundaries` table.
- Dynamic resolution-7 requests accept all framework query parameters and
  discard non-matching cells before serializing GeoJSON.

## Rebuilding framework data

The builder normalizes source fields, repairs invalid ecoregion geometry,
creates catalogues, and writes reusable geometry partitions:

```bash
uv run python ark_pipeline/builders/boundary_frameworks.py catalogue \
  app/static/data/boundaries/admin1.geojson \
  --output app/static/data/boundary-catalogs/admin1.json \
  --partition-field parent_code \
  --partition-dir app/static/data/boundary-geometry/admin1 \
  --partition-url-prefix /data/boundary-geometry/admin1 \
  --catalogue-partition-dir app/static/data/boundary-catalogs/admin1

uv run python ark_pipeline/builders/boundary_frameworks.py municipalities \
  path/to/DNK_ADM2.geojson path/to/DEU_ADM2.geojson path/to/SWE_ADM2.geojson

uv run python ark_pipeline/builders/boundary_frameworks.py ecoregions \
  data/sources/Ecoregions2017.zip

uv run python ark_pipeline/builders/boundary_frameworks.py eez \
  data/sources/MarineRegions_EEZ_v12.zip
```

For the global ADM2 layer, use the integrated command instead:

```bash
just data-boundaries
just data-prepare --tiles
```

Installing boundary data does not rewrite previously built maps. Rebuild map
metadata, coarse memberships and tiles after changing a framework, and reload
the serving database as appropriate. Restart an already-running backend so its
cached boundary indexes pick up the new generation. Pair generation is unaffected.

After changing a framework, run `just build-data` and `just db-load` so PMTiles,
map domains, Parquet memberships, and PostgreSQL remain in sync.

Required credits and canonical provider links are recorded in `NOTICE.md`.

## Adding other frameworks

Protected areas, OECMs, KBAs, and heavily overlapping management frameworks
should use a separate relation shaped like:

```text
resolution | h3_index | framework | boundary_id
```

Those relationships should be partitioned and queried on demand rather than
serialized as long ID lists on every map cell. A new framework becomes visible
in the filter UI by adding its manifest entry and configured catalogue. Entries
without an approved source stay visibly disabled instead of returning partial
or misleading results.
