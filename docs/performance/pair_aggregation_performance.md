# Pair aggregation: profiling and direct streaming

Historical harnesses and raw reports below are retained in the Git-ignored
`archive/` only. They are not required for the supported build or test suite.
Use `just data-benchmark` for current end-to-end measurements.

Use `just data-aggregate` after pair generation. It also runs automatically
through `just spatial`, `just data-build`, `just data-update` and
`just data-prepare`. No crosswalk is required for aggregation alone.

## What the pair-generation profiles taught us

The archived `profile_single_worker.py` separated geometry parsing, preparation,
conversion and native filling. The newer `diagnose_spatial_benchmark.py` added
repeated CPU/wall timing, RSS sampling, exact set comparisons and saved profiles.
Its production-v3 sample 984 spent 13.65 of 14.62 profiled kernel seconds in
native H3 filling, with approximately one core of utilization. The useful
lesson is to measure the expensive stage before optimizing Python overhead.

This aggregation work follows that method: fresh worker processes, three
alternating repetitions, separate cProfile runs without a competing RSS thread,
20 ms RSS sampling, DuckDB operator profiles, incremental JSON reports and exact
input/output relation digests. It compares the previous ordered `list()` path,
`list_sort(list())`, streaming after global finalization, and direct streaming.

On the dense 3-million-pair sample, just replacing grouping with streaming
reduced the complete measured pipeline from 0.88 s to 0.74 s. Grouping alone
was not faster; the gain came from validating the sorted stream while writing,
which removed separate input and duplicate-list scans. Profiling also showed
that the global distinct intermediate and its rereads were substantial costs.

## Implemented path

1. Verify every configured archive's source/code identity and pair checksum.
2. Partition raw numeric pairs once by H3 base cell.
3. Externally sort each partition by `(h3_index, iucn_sis_id)`. DuckDB may spill
   this sort to the configured scratch disk.
4. Remove adjacent duplicates while forming Arrow species-list arrays. Check
   nulls, resolution, base cell, ordering and relationship totals in that pass.
   Batch boundaries and a cell spanning several batches are handled explicitly.
5. Derive distinct resolution-3 parent/species pairs within the same partition
   using native integer bit operations. Each base cell has disjoint parents, so
   the coarse lists are concatenated and sorted without another global union.
6. Publish a complete generation through `serving/current` after reconciliation.
   Per-partition checksum receipts support interruption and resumption.

This avoids the global resolution-7 deduplication table/file and the flat
resolution-3 intermediate. The output schema remains
`(h3_cell UBIGINT, species_ids BIGINT[])`. Geometry/pair-generation code and its
fingerprint are unchanged. Only serving generations affected by aggregation
code changes rebuild. The optional `just spatial-finalize` diagnostic and the
low-level `ark_pipeline.cli.spatial_pairs` flat-relation workflow remain available.

## Results, 2026-09-02

All runs used one thread and disabled insertion-order preservation. Baseline
flat outputs use the previous implementation's 250,000-row Parquet groups.
Timings include deduplication, partitioning,
aggregation, relevant validation and ZSTD Parquet writes. Process startup,
source checksums, receipt publication and the independent exact audit are
outside the timing. Samples ran from warmed local SSD files, not the external
production disk. These are measured sample results, not a global wall-time estimate.

| Workload | DuckDB limit | Previous median | Direct median | Result |
| --- | ---: | ---: | ---: | --- |
| Dense: 3,000,214 pairs / 12,000 fine cells | 256 MB | 0.869 s | 0.451 s | 1.93× faster |
| Sparse: 3,000,000 raw pairs / 2,503,364 fine cells | 64 MB | 1.630 s | 0.944 s | 1.73× faster |
| Controlled 50% duplicates: 6,000,428 raw pairs | 256 MB | 0.941 s | 0.885 s | 1.06× faster |
| Dense: 12,000,000 pairs | 128 MB | Out of memory in fine list grouping, 3/3 runs | 1.908 s | Completed, exact |

Every completed result matched independently computed, sorted distinct input
relations at both resolutions. The 12-million-pair reference was verified even
though the previous list builder failed; matching new runs alone was not used
as the correctness reference. Regression tests also use the H3 library's parent
operation, including pentagons, independently of the production bit expression.

| Workload | Previous peak RSS | Direct peak RSS | Previous / direct peak spill |
| --- | ---: | ---: | ---: |
| Dense 3m | 414.2 MiB | 252.7 MiB | 0 / 0 MiB |
| Sparse 3m | 252.2 MiB | 284.3 MiB | 16.4 / 0 MiB |
| 50% duplicate 6m | 411.4 MiB | 295.1 MiB | 0 / 0 MiB |
| Dense 12m | Failed | 342.1 MiB | 137.9 MiB before failure / 39.6 MiB |

RSS values are medians of each run's highest sampled phase RSS, excluding the
final exact audit. DuckDB figures are its connection high-water marks. A DuckDB
memory limit is not a process RSS cap: Arrow, NumPy and runtime allocations sit
outside it. The sparse case was faster with slightly higher RSS. Memory savings
and speedups are therefore workload-dependent, not blanket claims.

The Arrow stage retains a 250,000-pair reader batch, roughly one million output
relationships and one unfinished cell. An exceptionally rich cell can exceed
those targets. Scratch space must accommodate raw partitions and external-sort
spill; highly duplicated input can require larger raw partitions than the former
deduplicated partitions. Full-release storage and throughput still need measurement.

## Reproduce and inspect

The machine-readable summary (local-only `archive/research/pair_aggregation_profile_2026-09-02.json`)
records input hashes, independent reference digests, timings, failure counts,
RSS, spill and phase medians. Detailed `.prof`, DuckDB JSON, logs and run reports
are retained locally under `data/spatial-test/aggregation-profiles/`.

The dense fixtures were sampled from existing base-12 species lists and expanded
to numeric pairs. The sparse fixture uses the first three million resolution-7
pairs in the old mammal file; old resolution-3 rows were excluded. The duplicate
fixture is two copies of the dense 3m sample. These are contrasting workload
shapes, not an unbiased population sample; no weighted full-run estimate is made.

```bash
# Accept current pair files, old mixed-resolution pairs, or serving-list files.
uv run python -m archive.research.profile_pair_aggregation sample \
  --source '/path/to/archive/res7_pairs.parquet' \
  --output /tmp/pair-sample.parquet --rows 3000000

uv run python -m archive.research.profile_pair_aggregation profile \
  --source /tmp/pair-sample.parquet --output /tmp/pair-profiles \
  --engine ordered-list --engine fused --repeats 3 \
  --memory-limit 128MB --threads 1 --cprofile
```

Use a new output directory for each matrix; results are saved after each phase
and run. Each run has a real subprocess timeout, avoiding the archived daemon
thread timeout's problem of leaving native work running after the timeout.
Use DuckDB's JSON operator timings for native SQL work and cProfile for Python
and Arrow glue. Summed cProfile entries are not the complete SQL wall time.
