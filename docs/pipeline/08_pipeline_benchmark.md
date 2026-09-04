# Pipeline benchmark after acquisition

Run the entire post-acquisition pipeline on the same stratified polygon fixture
used for the pair-generation runtime studies:

```bash
just data-benchmark                       # shared PIPELINE_WORKERS/auto setting
just data-benchmark --workers 4           # one budget for the compute stages
just data-benchmark --max-per-bin 10      # shorter, deterministic stratified subset
just data-benchmark --dry-run             # show stages and paths without building
just data-benchmark --ui rich             # force the dashboard, including redirected output
just data-benchmark --fresh               # new calibration instead of resuming an interruption
```

The default is the existing 1,000-polygon fixture at
`data/spatial-test/benchmark-samples/iucn-polygons-stratified-1000.parquet`.
Its source hashes must match the acquired archives. Current row exclusions are
applied before processing; the present fixture retains 997 polygons under v3.
Because it was selected under v2, estimates carry a warning about newly eligible
polygons that are absent from that sample. `--rebuild-sample` creates a new
1,000-polygon fixture using the original deterministic size-band sampler and
the current row policy. A missing fixture is also built automatically. The
original fixture is preserved. `--sample /path/to/sample.parquet` selects another
fixture with its companion `.json` metadata. `--max-per-bin N` retains at most N
ordinary hash-selected rows per band plus the forced smallest/largest fixtures;
it does not simply take the first N polygons.

Each fresh run uses a **new directory** under
`GLOBAL_DATA_ROOT/benchmarks/pipeline/<timestamp-id>/`. Override it with
`--output-root /path/to/a/new/directory`. Repeating the same command resumes a
compatible interrupted benchmark automatically; an explicit output directory can
also resume that run. Completed benchmarks are never overwritten. Sources are
read in place. All generated pairs, lists, crosswalks, metadata, databases,
boundary catalogues, scratch files and tiles stay inside that directory.
The command does not download sources, load PostgreSQL, or select benchmark
outputs for the live application.

This is an end-to-end serving benchmark for a sampled **polygon pair input**,
not a full benchmark of every spatial source route. It does not stream the 17
point archives, normalize the 14 HydroBASINS relationship archives, cover the
referenced v1c basins, or execute the basin/species join. Those operations run
in `just data-build` with resumable receipts and measured dashboard progress,
but they currently have no full-build ETA prior. Do not add the polygon estimate
to a release schedule as if it included those phases.

## Live dashboard and interruption

The Rich dashboard is automatic in a terminal. `--ui rich` forces it and
`--ui plain` selects line output. It refreshes four times per second, showing
stage timings, benchmark priors, live remaining time, expected finish, and the
projected full-build total. System CPU/RAM and process-tree memory/CPU appear
alongside the effective worker counts. Unavailable system measurements are
labelled rather than reported as zero.

The `Done` column shows a measured-work percentage for the active stage, such as
`99.9%` alongside `996 / 997 polygons`. It is not a percentage of elapsed or
remaining time. Unknown progress shows a dash; a successful stage shows a check.
Work counts stay on one line, while per-worker operations remain in Live Activity.
Worker counts are configured limits; `Active range jobs` reports how many
polygon tasks are still running. Large polygons borrow idle slots for native
tile threads, under the same spatial worker budget. Validation, simplification
and final deduplication are still serial within each polygon. With `--workers 1`,
tile processing also remains serial.

A process-tree reading near one core can be expected during checksum/source I/O,
single-query setup, or the tail of a parallel stage after seven workers have
finished and one large polygon remains. It does not mean the configured worker
limit was ignored. The dashboard now exposes active polygon jobs and borrowed
tile slots so those cases can be distinguished. Parallel tile work is bounded by
the shared spatial budget and cannot oversubscribe eight polygon workers into
eight additional threads each.

Run remaining and Expected finish are calculated only when every unfinished
stage has an estimate. Otherwise the dashboard explicitly counts the unestimated
stages, rather than presenting a partial subtotal as a run forecast. Pair ETAs
use each active polygon's elapsed time and grid progress, with a lower bound
from the longest active job; eight configured workers cannot divide the time
needed by one remaining polygon. Metric ETAs also consider recent throughput
and the longest active partition. These remain planning estimates: grid tiles
and partitions can have uneven costs, and unmeasured finalization can extend a
stage.

Tile export reports the number of features streamed across all base cells,
separately from subsequent compiler zoom/index passes. A Live left value ending
in `+ ?` estimates the streaming phase while later compilation remains
unestimated. It does not become a full-run ETA. Streaming progress resets only
when entering a new explicit phase, not when compiler or worker messages arrive.
A small polygon sample can still cover most of the world's fine cells: the
2026-09-02 `22-30-48Z-2ae48975` run produced 90,033,236 resolution-7 cells from
997 polygons. Its interrupted tile stage had streamed about 1.05 million of
90.07 million coarse/fine features after 260 seconds; the logs do not separate
feature generation from compiler backpressure, so they do not establish which
of those was the bottleneck.

Progress events report actual downloaded/hashed bytes, archive batches, polygon
grid batches, individual polygon completions, pair writes, list batches, metric
partitions and compiler passes. A percentage for a polygon, SQL query or compiler
pass is explicitly local to that operation. Some native operations cannot report
an intermediate percentage; the dashboard continues updating elapsed time and
resource use, with their log messages in the activity panel.

Initial estimates come from the latest completed benchmark with matching source
and spatial-profile fingerprints. Use `--benchmark-report /path/to/benchmark-report.json`
to select one explicitly. Pair ETAs update independently within size bands,
keeping forced extremes separate from ordinary observations. Later stages use
actual cell/relationship counts and measured throughput. Unknown stages or
unrepresented size bands remain unestimated until enough evidence exists.
Worker-count scaling is an initial assumption, not a measured speedup.

`dashboard-state.json` is saved atomically every second and on Ctrl+C. It retains
completed stages, active time, observations, estimates and event-reader position;
`progress.jsonl` retains the event history. Repeating the command restores that
state without counting time spent paused. A lock prevents two processes from
resuming the same run simultaneously. Changes to sources, profile, code, sample,
boundary inputs or resource settings prevent incompatible state reuse.

On resume, completed benchmark stages retain their original measurements and
their output inventory is checked before continuing. Downstream stages retain
their normal receipt/checksum validation. The interrupted stage retries; an
unfinished polygon archive or sample pair stage may need recalculation, while
validated metric partitions and tiles can be reused. Earlier observations remain
available to the ETA model, but unpublished polygons are not counted as saved
output. Stage timings include earlier attempts and exclude paused time. Resumed
reports flag that partial-stage cache reuse can affect throughput; use `--fresh`
with a new output directory for a clean calibration.

## Measured stages

1. Verify/read all registered spatial archives and recalculate the size-band
   population. This measures full source I/O on the current machine without
   polyfilling the full dataset. The census reads native ring envelopes and
   streams attributes joined by source FID, avoiding full WKB export and Shapely
   allocation for ordinary polygons. Ring-to-hole classification is disabled
   only during envelope reading and restored immediately afterward; coverage
   generation retains its normal geometry handling. Checksums, schema/CRS checks,
   row exclusions and per-layer row reconciliation remain required. Null, empty
   and degenerate envelopes use the original geometry path to preserve exclusion
   and failure behavior. Memory holds envelope/FID arrays for one layer plus an
   attribute batch, rather than a batch of full polygon geometries. This still
   reads every archive and can take minutes even for a small sample.
2. Build the crosswalk from the complete acquired IUCN, GoaT and NCBI references.
3. Generate sample pairs with the production kernel and bounded worker queue.
4. Feed **those pairs** into fine/coarse species-list aggregation.
5. Prepare the acquired global ADM2 boundaries in the benchmark directory.
6. Build complete species metadata, then coarse serving data and fine metrics
   against the sample's lists, followed by input reconciliation and real PMTiles.

Complete reference tables preserve lineage/DNA context; they are not spatially
sampled. No previous global pair/list/metric output is substituted. All five
boundary inputs are required; explicit `--admin0`, `--admin1`, `--municipality`,
`--eez` and `--conservation-framework` files can be supplied. An explicit existing
municipality file excludes ADM2 preparation from the measured/estimated scope.

## Reports and interpretation

`benchmark-report.md` shows measured wall time and an estimated full-build time
for each stage, plus both totals. `benchmark-report.json` adds resource settings,
source/profile/code fingerprints, workload counts, models and size-band means.
Each stage has a log under `logs/`; `polygon-timings.jsonl` retains individual
kernel timings and output counts. Peak process-tree RSS is sampled when process
inspection is available; an unavailable measurement is null. Reports are saved
after each stage, including failure; a failed run does not present a full total.

Pair estimates weight per-band means by the current full polygon population,
retain observed worker utilization, and separately scale pair writing. Forced
extreme fixtures exercise every stage but are excluded from sample means unless
the whole band was sampled. A populated band with no representative observation
prevents a full total estimate. The full source scan is added once as an I/O
calibration; its extra bounds decoding makes this a planning approximation.
Crosswalk, boundary preparation and species metadata run at full reference size,
so their measured costs are added once without multiplication. Fixture creation
is reported as setup, outside the projected build total.

Downstream estimates extrapolate the observed duplicate rate, cell counts and
relationship counts, with physical H3 cell-count caps. The report compares cell
and relationship throughput for metrics, and linear versus N log N scaling for
aggregation/tiles. Those scenario ranges **are not confidence intervals**.
Global overlap, disk spilling, compiler behavior and worker utilization may
differ from the sample. The benchmark creates fresh outputs but does not clear
OS filesystem caches. The fixture includes very large polygons, so the full
1,000-row run can still require substantial time and disk space.

Worker configuration follows [the pipeline guide](01_data_pipeline.md). `--workers`
sets spatial processes, fine-metric processes, single-process DuckDB threads and
Tippecanoe threads. Metric threads per worker and the tile query helper default
to one; explicit stage overrides remain available. `--memory-limit` controls
DuckDB memory per process, not total memory or native geometry allocations.

## Census optimization measurement

On 2026-09-03, the previous WKB/Shapely census and the native envelope census
were compared on three complete acquired 2026-1 archives on the local ARM64 Mac
(macOS 26.6.2), reading from the T7. Both paths included fresh SHA-256 verification
and archive inspection. All 12,250 rows, every size-bin count and every exclusion
count matched under the current v3 profile.

| Archive | Rows | Previous | Native envelopes | Speedup |
| --- | ---: | ---: | ---: | ---: |
| Amphibians | 10,614 | 16.48 s | 13.38 s | 1.23× |
| Sharks, rays and chimaeras | 1,338 | 24.62 s | 3.56 s | 6.92× |
| Croakers and drums | 298 | 2.02 s | 1.36 s | 1.48× |
| Combined | 12,250 | 43.12 s | 18.30 s | 2.36× |

These are single sequential runs, with the previous path first and no OS cache
flush; they are not a full-global speedup claim. Versions were GDAL 3.11.4,
Pyogrio 0.12.1 and Shapely 2.1.2. A bounds-only control with ordinary topology
classification still took 23.23 s on sharks/rays, versus 24.10 s for the geometry
reader (excluding verification), identifying ring organization as the principal
avoidable work for that archive.

The implementation uses [Pyogrio's native bounds reader](https://pyogrio.readthedocs.io/en/latest/api.html#pyogrio.read_bounds)
with GDAL's documented [ring-organization setting](https://gdal.org/en/stable/user/configoptions.html#config-OGR_ORGANIZE_POLYGONS).
Skipping classification preserves the envelope of all ring coordinates, but is
not suitable for computing polygon area or cell coverage. The setting is scoped
to the dedicated census process and must not be used concurrently with other
GDAL work in threads. No additional Rust extension or build toolchain is required.
