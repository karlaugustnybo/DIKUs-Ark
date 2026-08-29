# PC data-manipulation forensics (sanitized)

Date: 2026-08-29

## Scope and handling

The inspected workspace is referred to as `<old-workspace>`. The external
GeoParquet volume is referred to as `<external-geodata-root>`. Personal absolute
paths are intentionally omitted.

Inspection was read-only. Raw/provider files were inventoried by filename and
size, and selected Parquet row counts and schemas were read from file footers
only. No source records were copied. The recovery branch contains source code,
configuration, and documentation only under `recovered/pc/`.

The old workspace was on `main` at `645e3ac946d6dd039d8cd4195d8ad90123003444`
(`Flatten EBP into root, organize scripts/ and docs/`). It was 3 commits ahead
and 91 behind its configured `origin/main`, with staged v2 pipeline additions,
several staged deletions, and untracked v3 work. The recovery target began at
`101c887` on `codex/mac-sync-2026-08-29` and already contained the current app,
PostgreSQL schema, global IUCN/GOAT matcher, H3 aggregation pipeline, and release
guard. The recovered files are therefore retained as historical implementation
evidence rather than merged into current production paths.

## Recovered source inventory

| Area | Recovered files | Purpose |
|---|---:|---|
| Pipeline v2 | 10 Python files | Manifest, tiling, spatial polyfill, H3 aggregation/rollup, IUCN+GOAT merge, ToL export |
| Pipeline v3 | 12 Python files | Benchmarking, exact/lossy polyfill kernels, out-of-core deduplication, res-7/res-3 metrics, partitioned species lists |
| Diagnostics | 2 Python files | Aggregate IUCN/GOAT name-match audit and spatial schema/count inspection |
| Legacy documentation | 8 Markdown files | Schemas, lineage, prototypes, visualization plans, global reset rationale |
| Environment/configuration | 5 files | Python version, dependency lock/project config, ignore rules, historical Just tasks |
| Recovery index | 1 Markdown file | Bundle scope, exclusions, and sanitization notes |

No SQL, R, shell, PowerShell, Jupyter, or marimo source files were present in
the old workspace inventory. Generated Python bytecode and JSON benchmark/
profiling outputs were excluded.

## Discovered inputs

| Logical location | Input | Evidence and handling |
|---|---|---|
| `<old-workspace>/BioDatasets/IUCN_Red_List/` | `taxonomy.csv`, `assessments.csv` | Referenced by v2 merge and diagnostic code; raw files not opened or copied |
| `<old-workspace>/BioDatasets/GoaT/` | `goat_dataset.tsv` | Referenced by v2 merge/ToL logic; not opened or copied |
| `<old-workspace>/BioDatasets/backbone/backbone/` | GBIF `Taxon.tsv` | Used to map canonical names to accepted GBIF IDs; not opened or copied |
| `<old-workspace>/BioDatasets/TOL/` | multiple TSV exports | Filtered to taxonomic lineages by v2 script 07; not opened or copied |
| `<old-workspace>/BioDatasets/` | EDGE species TSV and IUCN spatial ZIP/shapefile archives | Restricted/provider material; names inventoried only |
| `<external-geodata-root>/iucn_ranges_v2/` | 7 joined class-level GeoParquet files | Footer-only schema and row counts recorded below; files not copied |
| `<old-workspace>/data/denmark/` | historical generated Parquet outputs | Footer-only schema/count checks; files not copied |

The seven external GeoParquet inputs total 128,768 polygon rows and
39,847,904,626 bytes (37.1 GiB), verified from Parquet metadata:

| Class file | Rows | Size (GiB) |
|---|---:|---:|
| amphibians | 10,543 | 2.00 |
| fishes | 20,547 | 15.18 |
| freshwater groups | 52,565 | 9.92 |
| mammals | 13,238 | 1.46 |
| marine groups | 1,987 | 3.01 |
| plants | 15,836 | 3.88 |
| reptiles | 14,052 | 1.66 |

All seven shared a 35-column Arrow schema at inspection time:

- Identity/taxonomy: `id_no`, `sci_name`, `iucn_grouping`, `kingdom`,
  `phylum`, `class`, `order_name`, `family`, `genus`.
- Range/status: `iucn_category`, `presence`, `origin`, `seasonal`, `marine`,
  `terrestial` (source spelling), `freshwater`, `source`, `geom_wkb`,
  `h3_res3`, `h3_res7`.
- Joined scoring/sequencing: `redlistCategory`, `threat_score`,
  `dna_coverage_score`, `sampling_priority`, `assembly_level`, `match_method`,
  `sequencing_status`, `sample_available`, `sample_collected`, `in_progress`,
  `insdc_submitted`, `published`, `has_dna_species_level`, `genus_has_dna`,
  `family_has_dna`.

## Transformation lineage

### Pipeline v2

1. `01_create_unified_spatial.py` scans `spatial_*.parquet` parts and writes a
   manifest with `table_name`, `parquet_path`, and `row_count`.
2. `02_generate_tiles.py` creates resumable 10-degree global tiles and a
   Denmark validation tile in DuckDB (`tiles` and `tile_status`).
3. `preclassify.py` measures WKB size, vertex count, geometry type, and bbox
   diagonal to route polygons to fast or simplified processing.
4. `03_buffered_h3_polyfill*.py` validates/repairs geometries, optionally clips
   to Denmark, polyfills at H3 resolution 7, and stream-writes atomic
   `(h3_index, gbif_accepted_id)` intermediates. The historical optimized path
   simplified slow geometries by 0.01 degrees and optionally expanded a one-cell
   H3 ring.
5. `04_aggregate_to_geoparquet.py` globally deduplicates pairs, groups species
   IDs per res-7 cell, derives cell geometry, and writes GeoParquet.
6. `05_derive_res3_from_res7.py` maps children to res-3 parents, unions species
   lists, and regenerates parent-cell geometry.
7. `06_generate_merged_gbif_from_h3.py` builds a deterministic GBIF canonical
   name map, joins IUCN taxonomy/assessments with GOAT and EDGE, retains both
   sides through a full outer join, and filters to H3-present species.
8. `07_export_tol.py` collects genus-through-kingdom names from the merged
   species output and filters ToL TSV rows to matching lineages.

### Pipeline v3 reset

1. Read the seven already-joined GeoParquet sources with
   `presence = 1`, `origin IN (1, 2)`, and non-null geometry.
2. Benchmark exact and approximate polyfill strategies; split
   antimeridian-crossing polygons and stream raw `(h3_index, id_no)` pairs.
3. Use DuckDB `SELECT DISTINCT` out of core across class files and overlapping
   polygons; do not hold a global Python deduplication set.
4. Join distinct species IDs to metadata and calculate res-7 metrics.
5. Re-map distinct pairs to res-3 parents and recalculate metrics from the
   parent/species relation. Do not sum child counts or average child averages.
6. Persist a lossless species-list dataset partitioned by H3 res-2 parent for
   bounded drill-down queries.
7. Optionally delegate to the v2 raw IUCN+GOAT merge and ToL export.

The v3 metric schema is one row per H3 cell:

`h3_index`, `latitude`, `longitude`, `total_species`, six Red List category
counts, `missing_species_dna`, `missing_genus_dna`, `missing_family_dna`, and
`dna_coverage_score`.

## Historical output metadata

The following counts were read from Parquet footers only:

| Old output | Rows | Key schema |
|---|---:|---|
| `data/denmark/h3_res7_species.parquet` | 39,505 | `h3_index`, `gbif_accepted_ids[]`, `geom` |
| `data/denmark/h3_res3_species.parquet` | 30 | `h3_index`, `gbif_accepted_ids[]`, `geom` |
| `data/denmark/h3_merged_raw.parquet` | 813 | wide IUCN + GOAT + EDGE merge |
| `data/denmark/h3_tol.parquet` | 949 | ToL lineage/sequencing fields + `gbif_accepted_ids[]` |

Legacy notes cite earlier Denmark counts of 36,195 res-7 and 27 res-3 cells;
those values do not match the inspected footer counts and likely describe an
older build. No record-level reconciliation was performed.

## Comparison with the current repository

The current repository supersedes much of this work:

- `scripts/match_iucn_goat_global.py` and related GBIF bridge scripts implement
  the current taxonomy/crosswalk workflow.
- `app/build_h3_aggregate.py` and `docs/h3_aggregation_pipeline.md` implement
  the current base-cell-partitioned, two-stage H3 aggregation for much larger
  pair volumes.
- `backend/schema.sql` defines the current serving schema (`species`,
  `species_systems`, compact cell species lists, inverse species coverage,
  boundary memberships, and aggregate app statistics).
- `scripts/check_release.py` enforces the code-only release policy.

The recovered v2/v3 code remains useful for geometry kernel history,
Denmark-specific validation, old output schemas, and rationale for global
deduplication. It should not be placed on the current production import path
without manual reconciliation.

## Exclusions and sanitization

- Excluded all raw CSV/TSV/ZIP/shapefile/provider content, databases,
  Parquet/Arrow/PMTiles/GeoPackage files, generated datasets, logs, caches,
  temporary JSON results, and Python bytecode.
- No credentials or private-key patterns were found in the recovered source.
- Replaced the machine-specific external drive path with `ARK_GEODATA_DIR` and
  `<external-geodata-root>`.
- Removed embedded provider record IDs from profiling code and comments.
- Copied no notebook because none was present. Consequently there were no
  notebook outputs to strip or notebooks to execute.

## Unresolved manual steps

1. Confirm licensing and current provider terms before any use or redistribution
   of IUCN, GOAT, GBIF backbone, EDGE, or ToL inputs.
2. Decide whether any v3 geometry kernels outperform or cover edge cases absent
   from the current `app/build_h3_aggregate.py`; this requires private test data.
3. Reconcile the Denmark footer counts with the older documented counts using a
   licensed local dataset and explicit build versioning.
4. Validate the recovered v2/v3 pipelines only in an isolated environment with
   `ARK_GEODATA_DIR` and private data roots configured. Full execution was not
   attempted because it would read restricted source records and generate
   prohibited datasets.
5. The old workspace records staged deletion of `01_merge_datasets.py`,
   `scripts/fetch_goat.py`, `scripts/fetch_goat_remaining.py`, and
   `scripts/merge_iucn.py`; those absent working-tree files were not recovered.
