# Pipeline code map

Use the stable `just` commands from the repository root. Direct Python commands
use `uv run python -m ark_pipeline.cli.<module>` or
`uv run python -m ark_pipeline.builders.<module>`.

Modules use descriptive names, not execution-order numbers: stages can be
resumed independently and some have multiple callers. The sequence below is
the normal full-build order; implementation files are grouped by responsibility.

## 01 — Acquire sources

In `cli/`, `sources_sync` coordinates download/update and authorized manual
registration; `sources_acquire` manages manifests, validation and snapshots.
`sources_download_adm2`, `sources_download_iucn`, and
`sources_download_goat.mjs` are provider-specific download helpers.

Commands: `just download`, `just update`, `just data-status`.

## 02 — Resolve taxonomy

`cli/crosswalk_refresh.py` selects validated snapshots and publishes a reusable
crosswalk. `cli/crosswalk_match.py` implements the IUCN–GoaT matching rules.

These run automatically during `just data-build` and `just data-update`.

## 03 — Cover spatial sources and aggregate species

| Module | Responsibility |
| --- | --- |
| `cli/spatial_pairs.py` | Stream ranges/points/basin tables, reuse referenced basin coverage, and generate common H3 pairs |
| `cli/spatial_aggregate.py` | Resume direct pair-to-species-list aggregation |
| `cli/spatial_audit.py` | Audit the selected spatial row policy |
| `cli/spatial_validate.py` | Validate H3 input contracts |
| `spatial/coverage.py` | Production range/HydroBASINS polygon-to-H3 coverage kernel and profiles |
| `aggregation/pairs.py` | Streaming sorted-pair deduplication and reduction |
| `aggregation/species_lists.py` | Validated, resumable species-list publication |

Commands: `just spatial`, `just data-aggregate`, `just validate-h3`.

## 04 — Build serving artifacts

`cli/serving_prepare.py` orchestrates preparation; `cli/serving_metadata.py`
resolves registered metadata inputs. `cli/boundaries_prepare.py` installs ADM2
catalogues. The builders are named after their outputs:

| Builder | Output |
| --- | --- |
| `species_metadata.py` | Normalized species and ecosystem metadata |
| `source_database.py` | Normalized DuckDB source database |
| `coarse_cache.py` | Coarse-map snapshot, exports and score domains |
| `fine_metrics.py` | Resolution-7 aggregate partitions and compatibility exports |
| `administrative_boundaries.py` | Natural Earth administrative catalogues |
| `boundary_frameworks.py` | Additional boundary-framework assets |

`aggregation/metrics.py` provides native metric reduction. `spatial/boundaries.py`
provides jurisdiction membership; `spatial/paths.py` resolves boundary assets.

Commands: `just data-prepare`, `just data-boundaries`, `just global-prepare`.

## 05 — Export tiles

`cli/serving_tiles.py` records prepared inputs and publishes validated tile
generations. `tiles.py` streams batched tile features.

Command: `just data-tiles` (also included in `just data-build`).

## 06 — Benchmark and monitor

`cli/benchmark_sample.py` builds the stratified fixture;
`cli/benchmark_pipeline.py` runs it through the production stages.

`runtime/` contains `resources.py`, `progress.py`, `dashboard.py`,
`benchmark_estimates.py`, `forecasts.py`, `provenance.py`, and `checkpoints.py`.
It owns execution support, not data transformations.

Command: `just data-benchmark`.

For source requirements and configuration, see the
[ordered pipeline guides](../docs/README.md). Historical tools live in ignored
`archive/` and are not imported by this package. Renames change code fingerprints;
let the normal receipt checks decide which existing outputs need rebuilding.
