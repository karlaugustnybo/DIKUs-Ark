# Data pipeline

## Recommended workflow

Set `GLOBAL_DATA_ROOT` in `.env` to the disk that should hold sources, generated
pairs, and scratch files. Then use:

```bash
just data-build             # acquire sources and build everything through map tiles
just data-build --dry-run   # preview paths and commands without running them
just data-status            # read-only source and spatial-output summary
```

After a source release changes:

```bash
just data-update  # update due sources, then run the complete build through tiles
```

The complete command acquires sources, refreshes the species crosswalk, builds or
resumes range-polygon, point, and HydroBASINS pairs, aggregates fine/coarse species lists, prepares metadata
and metrics, then exports PMTiles. `data-prepare` remains available when pairs
are already generated. Existing receipts are checked before reusing outputs;
the coarse preparation builders currently rerun on each invocation.

The **crosswalk** is the table connecting IUCN SIS species IDs to GoaT/NCBI IDs
so genome evidence is attached to the right species. It records which source
snapshots were used to establish those matches. After acquisition, the complete
command automatically rebuilds it when IUCN, GoaT, NCBI, matcher code or DuckDB
changes; otherwise it reuses a generation after checksum verification. It uses
the matcher's existing deterministic exact-name/synonym and lineage rules.
Ambiguous and near-name matches remain unresolved, with no accepted taxon ID.
They are saved in `unresolved_candidates.parquet` for optional review. Old API
review evidence is not silently reused; automatic coverage can therefore be
lower than a manually enriched crosswalk.

Crosswalk generations live under `GLOBAL_DATA_ROOT/derived/iucn-goat-crosswalk/`.
The build verifies species-ID and row reconciliation before selecting `current`,
and pins that generation throughout preparation. Failed refreshes preserve the
previous generation. Existing manually reviewed files are retained.
To use a particular reviewed crosswalk instead of automatic matching, run
`just data-build --crosswalk-mode require-current --crosswalk /path/to/crosswalk.parquet`.
That mode stops on stale provenance before expensive processing. See the
[crosswalk workflow](06_iucn_goat_global_crosswalk.md) for review and enrichment.

`GLOBAL_PREVIEW_ROOT/prepare-report.json` records completed commands, errors,
and the pinned source generation. Database loading and starting the app remain
separate from generating the data.

Global ADM2 map boundaries are included in source acquisition. They can also be
installed independently with `just data-boundaries`; no pairs or species crosswalk
are required. Global map preparation runs that step automatically. The current
snapshot covers 49,308 areas in 180 countries, with missing-country coverage
reported explicitly. See [boundary filtering](../reference/boundary_filtering.md).

Acquisition exits with code 2 when files or account-holder authorization are
still required. The chained build stops there and prints the action-plan path.
After placing the authorized files, rerun the same command. Use `just spatial`
to resume only the processing stages without checking for source updates.

`data-status` reads manifests, receipts, and file sizes. It does not download,
build, or scan every byte. Its `present-unverified` label is intentional: build
commands verify checksums before reusing outputs.

In a terminal, `just data-status` shows a Rich overview with source counts,
archive output states, spatial-stage readiness and the next command to run.
Use `just data-status --json` for the complete machine-readable report, or
`just data-status --ui rich` to force formatted output. Redirected output is JSON
by default. This is a one-shot snapshot; it stays visible after the command exits.

## Benchmark after acquisition

Run `just data-benchmark` to carry the existing stratified polygon fixture through
pairs, lists, metadata, coarse/fine metrics and PMTiles. It reports measured
stage timings and projected full-build timings, including a total, in a separate
directory under `GLOBAL_DATA_ROOT/benchmarks/pipeline/`. The shared `--workers`
setting and stage overrides apply here too. Use `--max-per-bin 10` for a shorter
stratified subset, or `--dry-run` to preview the command. See the
[benchmark guide](08_pipeline_benchmark.md) for sample provenance, estimates and outputs.
The fixture estimates the polygon route only. Point CSV conversion,
HydroBASINS normalization/indexing, and the basin/species join are visible as
measured production-dashboard work but remain outside the projected benchmark.

## Live progress and resume

`just data-build`, `just data-update`, `just data-prepare` and `just data-benchmark`
show a Rich dashboard automatically in a terminal. Use `--ui rich` to force it
or `--ui plain` for ordinary logs. The display includes stage progress, elapsed
and remaining time, benchmark priors, effective worker counts, system CPU/RAM
and pipeline process-tree usage. The dashboard fills the terminal, adapts when
resized, and uses extra height for live activity. It restores the normal terminal
on completion or Ctrl+C.

ETAs combine a compatible completed benchmark with observations from the current
run: polygon size bands and writing cost, source-archive/basin progress, actual cell/relationship counts, and
live throughput. Missing evidence displays an unknown estimate. Query and
compiler percentages describe the current operation, not the entire stage.
See the [benchmark guide](08_pipeline_benchmark.md#live-dashboard-and-interruption)
for the model and its limits.

Rich build progress is saved every second and immediately on Ctrl+C under
`GLOBAL_PREVIEW_ROOT/runs/<run>/dashboard-state.json`, beside `progress.jsonl`
and per-command logs. Repeat the same command to restore compatible unfinished
history, timings and learned estimates. Sources, code, profile and configuration
must still match. The build revalidates outputs through its existing receipts;
unfinished archives may restart, while completed archives and metric partitions
remain reusable. Paused time is excluded. `--fresh` starts fresh dashboard
history while retaining the production pipeline's normal output validation/reuse.
Benchmark resumes also retain completed stage measurements in their isolated run
directory; `--fresh` starts a new benchmark.

## Connect the new output to serving

After generating pairs, run the aggregation itself with:

```bash
just data-aggregate
```

This verifies all configured archive outputs, partitions their raw pairs once,
and builds deduplicated resolution-7 species lists plus distinct resolution-3
membership. It needs no metadata crosswalk. It also runs automatically through
`just spatial`, `just data-build`, `just data-update`, and `just data-prepare`.
`just spatial-export` is a compatibility alias for the same stage.

The combined aggregation avoids writing and rereading a global deduplicated
pair file. DuckDB sorts each base-cell partition with disk spill available;
Arrow/NumPy then remove adjacent duplicates, form lists, and validate the stream
in bounded batches. Coarse membership is deduplicated within each base cell,
whose resolution-3 parents cannot overlap another base cell's parents. The
geometry kernel and its fingerprints are unchanged, so this upgrade reuses
current pair outputs. See [the aggregation profiles](../performance/pair_aggregation_performance.md).

To resume after pair generation without repeating source acquisition:

```bash
just data-prepare --crosswalk-mode refresh --dry-run  # inspect the paths and commands
just data-prepare --crosswalk-mode refresh          # automatic matches, lists, metadata and metrics
just data-prepare --crosswalk-mode refresh --tiles  # continue through the full PMTiles archive
```

The full `data-build` and `data-update` commands already include these steps.
`data-prepare` resumes pair aggregation, automatically selects the active
profile's `serving/current`, then runs the existing `global-prepare` builders.
It pins the published generation for the complete preparation run. It checks
crosswalk provenance before aggregation and does not rerun polygon filling.
`GLOBAL_CROSSWALK_PATH` selects the crosswalk in the default `require-current`
mode; `refresh` automatically selects a managed crosswalk. `SPATIAL_PROFILE`
remains configurable. An existing
list pack can be selected with `just data-prepare --h3-root /path/to/pack`, which
skips pair aggregation. Otherwise this command uses the selected spatial profile,
even if `GLOBAL_H3_ROOT` still points to an earlier pack.

The report is saved as `GLOBAL_PREVIEW_ROOT/prepare-report.json`. It records
completed commands, effective resource settings and the exact source generation.
The shared worker setting and its overrides are described below. The DuckDB
memory limit is per process and does not cap Arrow/NumPy allocations; multiple
metric workers multiply that limit.

Successful preparation also writes `GLOBAL_PREVIEW_ROOT/prepared-inputs.json`,
binding coarse and fine products to the verified source generation. Use
`just data-tiles` to build or reuse the full static archive from that record.
`just global-res7-tiles` is an alias. The final archive and metadata are published
together under `GLOBAL_PREVIEW_ROOT/tiles/current/`. The default uses a single
streamed compilation; `just data-tiles --checkpoint-shards` adds resumable
per-base-cell tiles at the cost of a merge pass and extra disk space. See the
[tile profiling and publication guide](../performance/tile_export_performance.md).

The spatial workflow now exports the format consumed by the existing serving
builders: `(h3_cell UBIGINT, species_ids BIGINT[])`. IDs remain IUCN SIS IDs.
It produces one resolution-3 file and resolution-7 `base_*.parquet` partitions.

After the first successful run, set the following in `.env`, using your data
root (and your profile ID if you selected a different profile):

```dotenv
GLOBAL_H3_ROOT=/path/to/Ark-IV_data/derived/iucn-richness-any-touch-v3/serving/current
GLOBAL_CROSSWALK_PATH=/path/to/Ark-IV_data/derived/iucn-goat-crosswalk/current/iucn_goat_crosswalk.parquet
```

Then load the generated data into PostgreSQL and start the app:

```bash
just global-prepare       # only if not already completed by data-build/data-prepare
just global-db-configure  # once, if the serving database does not exist
just global-db-load
just start
```

The metadata builder automatically reads the registered IUCN assessment and
GoaT snapshots. It checks `match_summary.json` beside the crosswalk against the
registered taxonomy, assessments and GoaT hashes. A changed source requires a
rebuilt crosswalk; it cannot silently reuse an older join. The complete commands
handle rebuilding automatically. See [the crosswalk workflow](06_iucn_goat_global_crosswalk.md)
for optional review and enrichment. Setting up PostgreSQL remains explicit.

GBIF name enrichment and EDGE group labels are optional. Existing conventional
files are detected; explicit `GLOBAL_GBIF_SPECIES_PATH` and
`GLOBAL_EDGE_SPECIES_PATH` overrides must exist. Missing optional inputs are
reported. Species identities and map coverage do not depend on those enrichments.

Existing prebuilt packs keep working: without `GLOBAL_H3_ROOT`, serving reads
`GLOBAL_DATA_ROOT/h3_aggregated`. Without registered metadata snapshots, it uses
the existing `IUCN_Red_List/assessments.csv` and `TOL/tol_species_all_ranks.tsv`.
Set `GLOBAL_H3_ROOT` consistently for preparation, loading and running; rebuild
both coarse and fine products when changing source generations.

## Resume and publication

Each archive, temporary pair partition set, and completed list partition has a
checksum receipt. Aggregation verifies the current archive source identities,
code, schemas and complete archive set. It reconciles raw rows, exact duplicates
removed, fine relationships and distinct coarse membership before publication.
Interrupted runs reuse completed fine/coarse partitions. A missing or corrupt
temporary pair partition causes repartitioning while preserving valid lists.

Completed export partitions survive interruption. A complete generation lives
under `serving/generations/`; `serving/current` is an atomically replaced symlink
that points to it only after all outputs reconcile. Earlier generations remain
available, and temporary pair partitions are removed after successful export.
Budget disk space for raw pair partitions, sort spill and retained generations.
Very duplicate-heavy input can make the raw partitions larger than the former
deduplicated partitions. The DuckDB limit governs its sort, not total process
RSS; Arrow holds a reader batch, roughly one million output relationships and
one unfinished cell. See the profile report for measured RSS and spill usage.

This is publication of local H3 source lists, not a live application deployment.
The serving builders and database loader still run explicitly. Fine-cell
aggregate receipts include the source partition, species/system metadata, code,
and dependency hashes, so a DNA or threat-status update cannot reuse stale
scores. Existing aggregate files without these receipts rebuild once; later
unchanged runs reuse verified partitions. Full-scale
wall time, memory, and disk use for the new export need measurement on the
release hardware; synthetic integration tests do not establish global throughput.

## Individual stages and configuration

| Command | Purpose |
| --- | --- |
| `just data-build` | Acquire sources, refresh matches, build pairs/lists, metadata, metrics and tiles. |
| `just data-update` | Update due sources and run the same complete workflow. |
| `just data-build --dry-run` | Preview the complete workflow without downloads or builds. |
| `just download`, `just update` | Acquisition only. |
| `just spatial-doctor` | Inspect source archives and required attributes. |
| `just spatial-build` | Build/resume archive pair relations. |
| `just data-aggregate` | Direct pair deduplication and fine/coarse species-list aggregation. |
| `just spatial-export` | Alias for `data-aggregate`. |
| `just spatial-finalize` | Optional diagnostic: materialize flat distinct res7/res3 pair relations. |
| `just spatial` | Build/resume pairs, then run direct aggregation. |
| `just data-prepare` | Aggregate pairs, then prepare metadata, coarse map and fine metrics. |
| `just global-prepare` | Both coarse and fine serving products. |
| `just data-prepare --tiles` | Complete post-pair pipeline through the static PMTiles archive. |
| `just data-tiles` | Build/reuse PMTiles from the recorded preparation. |
| `just data-boundaries` | Acquire/install global ADM2 geometry and country catalogues. |
| `just global-res7-tiles` | Alias for `data-tiles`. |

## Workers and threads

Use one setting for the compute stages:

```bash
just data-build --workers 4
just data-update --workers 4
just data-build --workers 4 --dry-run
```

Or save `PIPELINE_WORKERS=4` in `.env`. `PIPELINE_WORKERS=auto` (the default)
chooses a conservative count from CPU and memory headroom, capped at eight.
On an 8 GiB Mac under ordinary desktop load this generally selects one.
The count is resolved once at the start of a complete run and passed to every
stage. Independent stage commands also read `PIPELINE_WORKERS`.

| Stage | With `--workers N` | Optional CLI override | Environment override |
| --- | --- | --- | --- |
| Range and referenced-basin filling | N shared CPU slots across geometry processes and tile helpers | `--spatial-workers` | `SPATIAL_WORKERS` |
| Fine metric partitions | N processes | `--metric-workers` | `RES7_WORKERS` |
| Crosswalk, basin-table normalization/join, pair aggregation, metadata and coarse SQL | N DuckDB threads | `--duckdb-threads` | `DUCKDB_THREADS` |
| Coarse and fine tile compilation | N Tippecanoe threads | `--tile-threads` | `TIPPECANOE_MAX_THREADS` |
| DuckDB/Arrow inside each metric worker | 1 thread | `--metric-threads` | `RES7_THREADS` |
| Fine tile query helper | 1 DuckDB thread | `--tile-duckdb-threads` | `TILE_DUCKDB_THREADS` |

The one-thread per-worker default avoids N workers each creating another N
query threads. Compiler and feature-generation work can overlap, so this is
configuration of worker pools, not a strict operating-system CPU quota.
Acquisition, boundary installation and Python-only feature generation remain
sequential; the setting does not change application API workers.

Precedence is stage CLI flag, shared `--workers`, stage environment override,
`PIPELINE_WORKERS`, then automatic selection. Per-worker/helper threads default
to one instead of inheriting the shared count. This means `--workers 4` also
overrides an older `DUCKDB_THREADS=1` in `.env`. For a smaller metric pool:

```bash
just data-build --workers 4 --metric-workers 2
```

`--workers auto` recalculates the shared default for one run. The `resources`
section of `--dry-run` and `prepare-report.json` records the effective counts.
Memory limits remain separate: `DUCKDB_MEMORY_LIMIT` is per process, so four
metric workers with a 4 GB limit can reserve substantially more memory than
one. Arrow buffers, native geometry and the tile compiler are additional.

`SPATIAL_PROFILE` selects an active profile. `SPATIAL_WORKERS`,
`DUCKDB_MEMORY_LIMIT`, `DUCKDB_THREADS` and `DUCKDB_SCRATCH_DIR` are optional
resource overrides. If omitted, spatial processing chooses memory-aware limits
and uses scratch space beneath `GLOBAL_DATA_ROOT`. `GLOBAL_PREVIEW_ROOT` also
defaults beneath that root. CLI options override environment defaults and common
options work before or after the subcommand.

See [acquisition](02_data_acquisition.md), [spatial details](04_spatial_rebuild.md),
and [serving](07_global_serving_pipeline.md) for schemas and detailed procedures.

`aggregation-report.json` beneath the profile's derived directory records the
selected serving root, cell/relationship totals, duplicate count and reuse.
The lower-level `ark_pipeline.cli.spatial_pairs finalize/export/run` commands retain
the explicit flat-relation path for diagnostics and compatibility; ordinary
`just` workflows use the direct aggregator and do not require those files.

## Code locations

- `ark_pipeline/spatial/`: current coverage kernel, jurisdiction membership and boundary paths.
- `ark_pipeline/aggregation/`: pair reduction, serving-list publication and native metrics.
- `ark_pipeline/runtime/`: provenance, resources, progress, estimates and checkpoints.
- `ark_pipeline/cli/`: acquisition, orchestration, metadata, crosswalk and end-to-end benchmark commands.
- `ark_pipeline/builders/`: serving database, species, metrics and boundary builders.
- `scripts/`: release checks and local development utilities.
- `config/`: source inventories and active semantic profiles.
- `archive/` (local only, ignored by Git): superseded builders, prototypes, research tools, profiles and their historical tests.

The [pipeline code map](../../ark_pipeline/README.md) lists the stage-based CLI
names and artifact-based builders in execution order.

Use `just data-benchmark` for the supported benchmark. Its stratified sample
builder is `ark_pipeline.cli.benchmark_sample`; no archived code is required.
Module paths changed during repository cleanup; the `just` command names remain
stable. Code fingerprints deliberately invalidate old checkpoints and receipts
when affected code changes. Preserve the old outputs, and let normal validation
decide what can be reused; do not modify receipts to bypass these checks.
