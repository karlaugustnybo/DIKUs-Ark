# Global source data and record links

The global preview is a joined product. The application species key remains
the IUCN SIS/internal taxon ID; it must never be relabelled as a GBIF key.
Source-specific identifiers travel beside it so every contextual link is exact
and omitted when its source has no defensible match.

## Required inputs

`just global-build-preview` expects these snapshot files below
`GLOBAL_DATA_ROOT` (the default is the project's mounted external disk):

| Relative path | Purpose |
|---|---|
| `IUCN_Red_List/assessments.csv` | Scientific names, assessment IDs, Red List status, and systems |
| `TOL/tol_species.tsv` | GoaT/NCBI lineage and sequencing evidence |
| `gbif_backbone_species.tsv` | Accepted GBIF taxon IDs for exact record links |
| `2024_EDGE_species_external_with_gbif.tsv` | Global EDGE ranks and groups |
| `h3_aggregated/h3_res3_species_global_merged.parquet` | Global overview distributions |
| `h3_aggregated/res7_merged_parts/` | Fine distributions served per visible tile |

The reviewed IUCN–GoaT crosswalk is generated locally at
`data/exports/iucn_goat_global/iucn_goat_crosswalk.parquet` and is ignored by
Git. The builder fails fast when a required file is missing; GBIF and EDGE are
optional at the Python API level for synthetic tests, but the global Just recipe
supplies both.

## Identity and link policy

- IUCN links use SIS and assessment IDs when both are available. A name search
  is the only fallback when an assessment ID is absent.
- GBIF links use the accepted taxon ID produced by an unambiguous, exact
  canonical-name match in the supplied backbone snapshot. Ambiguous or absent
  matches do not get a link.
- GoaT links use the accepted NCBI taxon ID from the reviewed crosswalk.
- EDGE values join by IUCN Red List ID and keep the best numeric EDGE rank when
  the input contains more than one row.

The current mounted build has 171,625 application species rows, including 21
explicit placeholders for IDs present in H3 data but missing from the supplied
IUCN release. It has 158,972 exact GBIF IDs, 126,285 accepted GoaT/NCBI IDs,
and 3,867 EDGE records. Missing GoaT coverage is represented as the mutually
exclusive “GoaT Data Deficient” DNA category rather than guessed from a name.

## Distribution and highlighting scale

The resolution-7 source contains more than 30 billion cell–species
relationships, so it is neither expanded into PostgreSQL nor inverted. The
build exports one compact inverse list per species at resolution 3. When a
species is highlighted over a fine map, the browser maps visible resolution-7
cells to their resolution-3 parent. This keeps the interaction global and
exact at the resolution reported by the interface without duplicating the
large res-7 dataset.

## Publication boundary

The code repository must not contain any file from the required-input table,
the crosswalk, database dumps, PMTiles/Arrow snapshots, H3 aggregates, or
species-list partitions. This is deliberately stricter than testing the file
extension: a converted or aggregated file may remain reconstructable and may
inherit its source restrictions.

IUCN prohibits reposting its raw/tabular/spatial data without written
permission and limits derivative and commercial use. ZSL's EDGE terms prohibit
copying, publishing, transmitting, or redistributing its site contents and data
without express permission. Therefore Ark-IV publishes neither source's rows or
rankings in Git. Protected Planet is also source-required because its WDPCA
terms prohibit redistribution and sublicensing.

The About Data page provides record links for interpretation, but a link does
not grant permission to redistribute the linked fields. Before publishing any
hosted data snapshot, follow the evidence requirements in `DATA_POLICY.md` and
the citations in `NOTICE.md`.

Relevant references:

- <https://api.iucnredlist.org/>
- <https://www.edgeofexistence.org/terms-and-conditions/>
- <https://www.gbif.org/dataset/d7dddbf4-2cf0-4f39-9b2a-bb099caae36c>
- <https://github.com/genomehubs/goat-data>
