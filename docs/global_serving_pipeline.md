# Global serving pipeline

This document describes the integration boundary between the global IUCN/GoaT
species work and the H3/map build. The H3 validation and tile plumbing can run
before the final crosswalk is available.

## Inputs

The completed global H3 aggregates have this schema:

| Column | Type | Meaning |
|---|---|---|
| `h3_cell` | `UBIGINT` | Numeric H3 index |
| `species_ids` | `BIGINT[]` | IUCN `internalTaxonId` values |

The source-database adapter also continues to accept the prototype schema
`(h3_index VARCHAR, gbif_accepted_ids VARCHAR[])`.

## Crosswalk handoff

Set `H3_ID_CROSSWALK_PATH` to a Parquet file containing:

| Column | Required | Meaning |
|---|---|---|
| `source_species_id` or `iucn_sis_id` | yes | IUCN `internalTaxonId`, castable to text |
| `app_species_id` | optional | Stable identifier used by `SpecInfo`; defaults to the IUCN ID |
| `match_method` | recommended | Exact name, synonym, manual, etc. |
| `match_confidence` | recommended | Machine-readable confidence |
| `goat_taxon_id` | recommended | Matched GoaT/NCBI taxon |
| `gbif_accepted_concept_key` | optional | Exact GBIF taxon identifier when available |

The crosswalk currently produced by `scripts/match_iucn_goat_global.py` is
accepted directly: `iucn_sis_id` becomes the stable application ID and
`matched_ncbi_species_taxid` supplies the GoaT link. For compatibility, the
adapter also accepts the legacy `gbif_accepted_id` column as the application
target when `app_species_id` is absent. That legacy field is an internal app
key, not necessarily a GBIF identifier. There must be one non-null target per source ID.
Many-to-one targets are rejected because they would silently collapse distinct
IUCN species inside an H3 cell.

## Pre-crosswalk validation

Structural validation does not need species metadata:

```bash
uv run python -m app.validate_h3_input \
  --res3 "/path/to/h3_res3_species_global_merged.parquet" \
  --res7 "/path/to/h3_res7_species_global_merged.parquet" \
  --output data/validation/global-h3.json
```

The default pass includes:

- cell and relationship counts;
- null H3 indexes and null species lists;
- H3 indexes whose encoded resolution does not match the file.

Add `--deep` to scan for duplicate H3 rows and duplicate species IDs within a
cell. This is quick for res 3 but is intentionally separate for the 96-million
row res-7 file. The final source build always performs the deep checks before
publishing.

## Source and serving builds

After the crosswalk and global species tables are ready:

```bash
H3_RES3_PARQUET=/path/to/h3_res3_species_global_merged.parquet \
H3_RES7_PARQUET=/path/to/h3_res7_species_global_merged.parquet \
H3_ID_CROSSWALK_PATH=/path/to/iucn_goat_crosswalk.parquet \
uv run python app/build_db.py --overwrite

uv run python app/build_cache.py \
  --rebuild-aggregates \
  --skip-expanded-cell-species
```

`build_db.py` writes `data/validation/source-validation.json` before replacing
the existing source database. It refuses to publish a build with unmatched
relationships, duplicate mappings, missing species metadata, or dropped cells.

`build_cache.py` writes `data/validation/build-validation.json`. It checks that
the all-system cell and relationship totals equal the normalized source after
the `SpecInfo` join.

The global command intentionally skips the expanded PostgreSQL
`cell_species` export. Expanding 30,883,702,920 relationships would be
impractical. `cell_species_lists.parquet` preserves the compact per-cell lists;
the selected-cell endpoint still needs a partition-aware serving path before
the global deployment is complete. Without the skip flag, the build refuses
to expand more than 100 million relationships.

## Global scaling changes

- There is no `H3Centroids` table or Denmark border-intersection loop.
- Habitat layers are derived from each species' IUCN systems.
- IUCN IDs are retained as the global application key, so normalizing the H3
  files does not expand and regroup their 30+ billion relationships.
- Aggregation runs one H3 base cell at a time. Within each partition, every
  species list is expanded once and all four system aggregates are calculated
  in that pass.
- Before a completed partition is published, the builder verifies source and
  output cell counts, lossless relationship totals, H3 resolution and base-cell
  placement, duplicate cell rows, and internal metric consistency. The
  temporary file is deleted on failure and never replaces the last valid
  partition. Current-schema partitions are revalidated when a build resumes.
- Tile rows are fetched from DuckDB in bounded batches.
- GeoJSON sequences are piped directly into Tippecanoe. The build does not call
  `fetchall()` or materialize temporary GeoJSON files.

H3 polygon boundaries are still produced incrementally with the H3 C-backed
Python package. Memory use is bounded, but a full 96-million-cell tile build
will remain CPU- and I/O-intensive and should be benchmarked by base-cell or
geographic slice before the production run.

## Verified baseline

On 2026-07-23:

| File | Cells | Cell–species relationships | Structural failures |
|---|---:|---:|---:|
| Global merged res 3 | 40,295 | 17,449,008 | 0 (deep) |
| Global merged res 7 | 95,984,189 | 30,883,702,920 | 0 (structural) |

The res-7 structural pass found no null cell IDs, null lists, or incorrectly
encoded resolutions. Its full duplicate-list audit remains an explicit
`--deep` operation because it is substantially more expensive.

The res-3 lists contain 74,352 distinct IUCN IDs. Of those, 74,331 occur in
the current merged IUCN taxonomy file and 21 do not. The missing IDs are:

```text
780, 7121, 7879, 7880, 8128, 10332, 20355, 34040, 34169, 40747, 44072,
61372, 153621, 165228, 165247, 177653, 195373, 195376, 63488699,
78777637, 232856397
```

These are an upstream-version mismatch, not a GoaT matching problem. The
global species-dimension build retains them as explicit placeholders so H3
coverage is lossless. Their source-specific identifiers and metadata remain
null instead of being guessed; the build report lists every placeholder.

The Denmark compatibility build also completed without loss:

| Resolution | Cells | Relationships | Dropped |
|---|---:|---:|---:|
| 3 | 30 | 11,688 | 0 |
| 7 | 39,505 | 9,394,667 | 0 |
