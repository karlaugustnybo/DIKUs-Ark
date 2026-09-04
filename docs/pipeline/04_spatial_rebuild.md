# Spatial rebuild

Historical harnesses and raw reports below are retained in the Git-ignored
`archive/` only. They are not required for the supported build or test suite.
Use `just data-benchmark` for current end-to-end measurements.

The spatial stage reads authorized IUCN polygon archives through GDAL Arrow,
point and basin-relation ZIP members through Arrow CSV, and referenced
HydroBASINS v1c geometries through GDAL. It does not materialize a full
GeoPandas or Pandas frame. Every route emits the same native
`(h3_index uint64, iucn_sis_id int64)` pair contract.

The production semantics are defined in
`config/spatial_semantics_iucn_richness_v3.toml`: a resolution-7 cell is
included whenever its closed polygon touches the closed decision geometry at
any point, including a shared edge or vertex. For large v3 ranges that geometry
may be simplified, so membership is approximate relative to the original map.
Holes, multipolygons, tiny ranges,
antimeridian ranges, invalid-geometry repair, and boundary contact have
regression fixtures. Resolution 3 is derived from included resolution-7 cells,
never independently from fine membership.

Eligible IUCN points map to exactly one containing resolution-7 cell. They are
not buffered or counted as repeated observations. Eligible HydroBASINS table
rows are deduplicated to `(hybas_id, species)` out of core. The numeric basin ID
selects its region and level; only referenced geometry FIDs are decoded, each
basin is covered once with the polygon any-touch kernel, and all 14 relationship
tables reuse that basin-cell index. A referenced ID missing from v1c is fatal.

The row policy follows IUCN's published 2021+ richness selection: Presence
codes 1 and 4, Origin codes 1, 2 and 6, and Seasonality codes 1, 2, 3 and 5.
This is a *potential richness* definition, not a claim that every included
range is confirmed currently occupied: Presence 4 means possibly extinct and
Seasonality 5 is uncertain. The conservative
`config/spatial_semantics_any_touch_v2.toml` remains available for analyses
limited to extant native/reintroduced ranges (Presence 1; Origin 1 or 2); it
does not impose a seasonality filter. Every included and excluded source row
keeps its original Presence, Origin and Seasonality values in the polygon row
audit. Point and relationship tables use compact per-member decision summaries
to avoid writing an audit copy of multi-gigabyte CSVs; normalized eligible
basin/species relationships and the registered originals remain available.

The exact direct kernel uses 10° partitions below 100 square degrees of bbox
area and 2.5° partitions at or above that threshold. The smaller large-range
partition was exact on all 50 tested tail polygons and reduced sequential fill
time because the native fill cost grows superlinearly with complex tile
geometry. It is not a simplification and does not alter polygon boundaries.

The v3 population-weighted benchmark estimates 25.90 CPU-hours for the bounded
production router. Ten cores at an assumed 80% effective utilization correspond
to 3.24 compute-hours, with a 2.33--4.18 hour bootstrap interval, before fixed
archive reads, pair writes and finalization. The production TOML enables `0.01°` decision
simplification only for these five largest bands. Its conservative WGS84
displacement bound is 1,116.94 m, below the required 2 km maximum. Fast
simplification is accepted only when it is valid and preserves the number of
disconnected polygon components; otherwise the kernel tries topology-preserving
simplification. Invalid results fall back to the original geometry. The row
audit records whether simplification was applied and its metre bound.

The metre value is a tolerance-derived budget, not a per-row measured ecological
accuracy. Equal component count does not prove component correspondence or
preservation of every hole. See the [methodology](../../methodology.md#7-h3-coverage-and-simplification)
and [assumption audit](../reference/roadmap_assumption_audit.md) for the required
post-fix calibration and remaining scientific acceptance criteria.

## Commands

For the complete resumable spatial workflow, use `just spatial`. It validates
inputs, builds archive pairs, and aggregates both resolutions directly into
the serving list format. `just data-build` also performs
acquisition first; `just data-status` is a read-only readiness check. The
[pipeline guide](01_data_pipeline.md) explains serving configuration.

The individual commands below remain available for diagnostics and calibration.

Run the cheap source and schema diagnostic before expensive work:

```bash
just spatial-doctor
```

Build or resume the per-archive pair stages for the selected profile:

```bash
just spatial-build
```

The command inherits `PIPELINE_WORKERS`, with `SPATIAL_WORKERS` as its geometry
override. The default `auto` chooses a conservative count from CPU and current
memory headroom (one worker on an 8 GiB machine under ordinary desktop load).
Complete builds also accept `just data-build --workers N`; see the shared
[resource settings](01_data_pipeline.md#workers-and-threads). Choose larger counts
after observing per-worker peak RSS on the target hardware. Arrow input is
capped at 128 source rows per batch, completed worker
results are consumed immediately, and large pair arrays are written in
250,000-row slices to avoid unnecessary whole-result copies. Point conversion
uses h3ronpy's native vector path. Basin relationship normalization and joins
use the configured spillable DuckDB memory/scratch limits rather than Python
sets of species relationships.

Each polygon, point, and relationship archive has a content-addressed receipt covering its input checksum,
semantic profile, relevant code, geometry dependency versions, schemas, and
output checksums. HydroBASINS relationship receipts additionally bind the nine
regional geometry archives. A readable Parquet file without a matching receipt
is rebuilt. Polygon rows retain row-sized audits; tabular sources reconcile all
rows into decision counts, and referenced basins retain per-geometry audits.

After all configured archives pass, aggregate pairs into serving lists:

```bash
just data-aggregate
```

Raw pairs from all three evidence routes are partitioned once by H3 base cell. A spillable numeric sort feeds
bounded native batches that remove exact duplicates and form species lists.
Resolution-3 membership is deduplicated per base cell, then combined; counts
are never summed across resolution-7 children. Receipts support resuming both
resolutions, and `serving/current` advances only after reconciliation.

This stage also runs through `just spatial` and `just data-prepare`.
`just spatial-export` is an alias. It reuses existing geometry/pair receipts.
Measurements and profiling commands are in the
[aggregation performance report](../performance/pair_aggregation_performance.md).

For diagnostics that specifically need flat distinct pair files, the separate
materialization step remains available:

```bash
just spatial-finalize
```

This stage uses DuckDB's out-of-core native distinct and derives H3 parents
with vectorized `uint64` bit operations. Its default memory limit reserves
headroom from both physical and currently available memory (typically 1–2 GB
on an 8 GiB machine); `DUCKDB_MEMORY_LIMIT` and `DUCKDB_THREADS` remain explicit
overrides. It rejects an incomplete archive set. For an explicitly partial
calibration build, invoke the module directly with `finalize --allow-partial`.

The direct aggregator does not need these flat files. The low-level Python
`ark_pipeline.cli.spatial_pairs export` command still accepts that compatibility path.
Both routes preserve IUCN IDs and do not perform metadata matching.

## Research and historical implementations

The supported end-to-end benchmark is `just data-benchmark`; its sample builder
is `ark_pipeline.cli.benchmark_sample`. Earlier hierarchy experiments are kept
in local-only `archive/research/spatial/`. Their results are described in
[the benchmark report](../performance/spatial_hierarchy_and_simplification_benchmark.md).

Superseded positive-area/centroid profiles, kernels, and calibration results
are preserved in local-only `archive/spatial/`. These directories are ignored
by Git and are not needed for builds or tests. The production runner accepts
the current any-touch kernels only.

Moving kernels and fixing the row audit changes the code fingerprint. Existing
outputs stay on disk, but the next build will regenerate stages whose receipts
no longer match; do not edit receipts to force reuse.
