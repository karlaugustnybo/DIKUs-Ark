# Documentation

Start with the [scientific and implementation methodology](../methodology.md)
for the complete pipeline, decision rationale, formulas, evidence and limitations.
The [roadmap assumption audit](reference/roadmap_assumption_audit.md) distinguishes
verified behavior from open scientific decisions.

## Pipeline — read in order

The [pipeline code map](../ark_pipeline/README.md) explains the package layout,
stage-based CLI names and artifact-based builders.

1. [Workflow and commands](pipeline/01_data_pipeline.md)
2. [Source acquisition](pipeline/02_data_acquisition.md)
3. [Authorized IUCN browser downloads](pipeline/03_iucn_browser_download.md)
4. [Spatial processing and rebuilds](pipeline/04_spatial_rebuild.md)
5. [Spatial row policy](pipeline/05_spatial_row_policy.md)
6. [IUCN–GoaT crosswalk](pipeline/06_iucn_goat_global_crosswalk.md)
7. [Global serving pipeline](pipeline/07_global_serving_pipeline.md)
8. [End-to-end benchmark](pipeline/08_pipeline_benchmark.md)

## Reference

- [Source inventory](reference/global_source_data.md)
- [Serving schema](reference/schema.md)
- [Boundary filtering](reference/boundary_filtering.md)
- [Pipeline change requirements](reference/data_pipeline_change_requirements.md)
- [Roadmap](roadmap.md)
- [Publication checklist and history blocker](publication_checklist.md)

## Performance evidence

- [Map rendering retrospective](performance/map-performance-retrospective.md)
- [Species search](performance/species_search_benchmark.md)
- [Pair aggregation](performance/pair_aggregation_performance.md)
- [Serving metrics](performance/serving_metrics_performance.md)
- [Tile export](performance/tile_export_performance.md)
- [Spatial hierarchy and simplification](performance/spatial_hierarchy_and_simplification_benchmark.md)

Historical profiling harnesses and raw reports mentioned in these documents are
kept under local-only `archive/research/`, not in the GitHub code release. Current
validation lives in `tests/`; use `just data-benchmark` for new end-to-end timings.
