# Serving metrics: native batch reduction

Historical harnesses and raw reports below are retained in the Git-ignored
`archive/` only. They are not required for the supported build or test suite.
Use `just data-benchmark` for current end-to-end measurements.

The current post-pair workflow starts with `just data-aggregate`, or
`just data-prepare` to include metadata and metrics. Pair/list aggregation now
uses the [combined streaming path](pair_aggregation_performance.md).
The archived PC scripts remain
historical reference: their per-pair string/parent conversions, polygon-attached
metadata, repeated deduplication, and old DNA scoring are not used by the new
serving build. Resolution 3 still comes from distinct parent/species membership,
so a species spanning several fine cells is counted once in the coarse cell.

`ark_pipeline/aggregation/metrics.py` replaces the expensive fine-cell metric reduction in
`ark_pipeline/builders/fine_metrics.py`. It follows the pair kernel's useful design choices:
native arrays, integer identifiers, immediate consumption, and bounded batches.

1. Evaluate the existing SQL metric predicates once per metadata species.
   NULL behavior and overlapping ecosystem membership are preserved.
2. Read Arrow batches of 256 cells and find species IDs in a sorted numeric
   lookup. Split unusually large batches around a target of 250,000
   relationships, without splitting an individual cell.
3. Gather one byte-valued metric at a time and reduce at list offsets. Only
   one relationship-sized metric array is needed at once; there is no expanded
   Python row list, text join key, or partition-wide metric hash table.
4. Buffer up to roughly 16,384 output cells per Parquet write and convert H3
   indexes to the existing string API format through the native H3 library.
5. Run the existing lossless publication checks before replacing a partition.
   Unknown IDs cannot acquire another species' flags; missing relationships
   fail validation. Receipts cover the new reducer and NumPy version.

The Arrow input batch can exceed the target relationship count for very rich
cells; the relationship target bounds reduction slices, not total RSS. DuckDB's
memory limit also does not govern Arrow/NumPy. Each parallel worker has its own
lookup and buffers. Output follows source cell order, which the current list
exporter sorts; metric values do not depend on that order.

## Measured sample, 2026-09-02

The sample comprises the first 12,000 cells from the existing base-12 partition:
3,000,214 relationships, using the existing species and ecosystem metadata.
Three runs alternate engine order with one DuckDB thread and a 750 MB DuckDB
memory limit. The measurements include aggregation, native lookup preparation,
and ZSTD Parquet writing. Shared metadata preparation and publication validation
are outside the timer for both engines.

| Implementation | Median | Individual runs |
| --- | ---: | --- |
| Previous SQL join and filtered counts | 3.55 s | 3.68, 3.55, 3.47 s |
| Arrow/NumPy batches | 1.10 s | 1.13, 1.09, 1.10 s |

The measured speedup is **3.23×**. Bidirectional `EXCEPT ALL` found zero differing
rows across all 164 metric columns. This sample does not establish global wall
time or peak memory. Geometry filling, pair finalization, list export, checksum
verification, coarse builds, and tile generation are separate costs.

The machine-readable report (local-only `archive/research/serving_metrics_benchmark_2026-09-02.json`)
records sample/metadata hashes, timings, and dependency versions. Reproduce on
another partition without modifying the input:

```bash
uv run python -m archive.research.benchmark_serving_metrics \
  --source '/path/to/res7_merged_parts/base_12.parquet' \
  --species data/global/species/species.parquet \
  --species-systems data/global/species/species_systems.parquet \
  --cells 12000 --repeats 3 --output-dir /tmp/ark-serving-benchmark
```

Regression tests compare the new reducer against the previous SQL expression
over every threat category, boolean/NULL DNA combination, and ecosystem mask.
They also cover oversized cells, batch boundaries, missing/noncanonical IDs,
interruption cleanup, receipt invalidation, and existing serving consumers.
