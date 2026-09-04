# From metrics to PMTiles

Historical harnesses and raw reports below are retained in the Git-ignored
`archive/` only. They are not required for the supported build or test suite.
Use `just data-benchmark` for current end-to-end measurements.

After generating pairs, the complete post-pair build is now:

```bash
just data-prepare --tiles --dry-run
just data-prepare --tiles
```

This pins one source generation, aggregates the pairs, builds species metadata
and coarse/fine metrics, records the verified preparation, and creates the static
map. A reviewed crosswalk matching the source snapshots is required.
To resume only the final export, use `just data-tiles` (also exposed as
`just global-res7-tiles`). It reads `GLOBAL_PREVIEW_ROOT/prepared-inputs.json`,
so no separate manual `GLOBAL_H3_ROOT` handoff is needed for tile export.

The final files are:

```text
GLOBAL_PREVIEW_ROOT/tiles/current/priorities.pmtiles
GLOBAL_PREVIEW_ROOT/tiles/current/map-metadata.json
GLOBAL_PREVIEW_ROOT/tiles/current/build-report.json
```

`current` is an atomic symlink to an immutable generation. Consumers needing
both files across multiple requests should resolve/pin the generation path.
Deployment and database loading remain separate. The old low-level
`ark_pipeline.builders.fine_metrics tiles` command remains available for diagnostics; its
direct output is not managed by these receipts.

## Profiling results

The diagnostic cProfile run spent 16.34 of 17.49 seconds on 60,000 boundary
lookups for 12,000 fine cells. Most time went into repeated intersections with
complex geographic boundaries. Each of the five frameworks also reconstructed
the same H3 polygon independently.

The new exporter prepares boundary geometries once and evaluates candidate
intersections in native Shapely batches. It builds each ordinary H3 ring once,
shares that geometry across frameworks, preserves catalogue order for overlapping
codes, and retains the established antimeridian clipping behavior.

Three alternating runs per implementation used fresh processes, a local SSD,
one DuckDB thread, and the same five real boundary catalogues. Separate cProfile
runs omitted the RSS sampler thread, following the pair-generation methodology.
The municipality input was the bundled three-country preview, before global
ADM2 installation. These results do not measure the larger global boundary index.
Feature timings include reading/sorting the metric sample, boundary assignment,
polygon generation, JSON encoding, and writing the stream.

| Measurement | Previous exporter | Batched exporter |
| --- | ---: | ---: |
| Median feature generation | 15.301 s | 2.762 s |
| Median boundary loading + feature generation | 18.589 s | 6.007 s |
| Median sampled Python peak RSS, including boundary loading | 430.7 MiB | 431.0 MiB |

Feature generation improved **5.54×**; including boundary loading, the measured
improvement was **3.09×**. All runs produced byte-identical GeoJSON, including
geometry, metric values, geographic codes and layer zoom ranges.

Compiling that stream separately with Tippecanoe 2.79.0, two threads and zooms
8–12 took **8.165 s** median (8.471, 8.165, 8.158 s), producing 49,030,607 bytes.
This is an isolated disk-input measurement, not an end-to-end speedup. Compiler
subprocess RSS was not sampled. Production streams directly into the compiler;
its feature timings include backpressure and overlap native compilation.

A complete managed sample build also passed: 12,000 fine cells, 343 distinct
coarse cells and 3,000,214 fine species relationships. Its initial shard-based
trial exposed a 13.74-second merge pass, erasing the cold-build feature gain.
Consequently, **the default streams all partitions through one compiler**.
Per-base-cell tile checkpoints are an explicit option for long, interruption-prone
runs where resumability is worth the extra merge and storage.
The subsequent single-pass smoke build completed the same 12,343-cell archive
in 14.82 seconds, including input verification, boundary loading, compilation and
map metadata. This was one functional run, not a repeated end-to-end benchmark.

The sample comes from an existing data pack, not a completed run of the new
global spatial profile. It is not geographically representative. These timings
do not establish global throughput, archive size, or a universal memory bound.
The earlier metric benchmark is in [serving_metrics_performance.md](serving_metrics_performance.md).

## Publication and reuse

`global-prepare` records the source generation, species tables, coarse database,
map metadata and every source/metric partition. Coarse cell/species counts must
match the source lists; fine partitions require current aggregation receipts.
Tile export verifies checksums and current metric code/dependencies, and rejects
changed inputs or incomplete fine coverage. All five boundary inputs are required;
alternative catalogues can be supplied explicitly to `data-tiles`.

Both modes sort one base cell at a time, stream GeoJSON through a pipe, verify
outputs, and switch `current` only after the archive and metadata are complete.
Unchanged complete generations are reused after checksum verification. Failed
builds leave the previously selected generation intact. A single-pass build
restarts compilation after interruption.

For persistent tile checkpoints:

```bash
just data-prepare
just data-tiles --checkpoint-shards
```

This produces one coarse PMTiles shard and one per fine H3 base cell, then merges
them with `tile-join`. Shard receipts cover the metric input, boundaries, code,
dependencies, compiler binaries and compiler thread setting. Completed shards
survive interruption and remain reusable when only unrelated partitions or map
metadata change. A failed merge can restart from those shards.

H3 IDs remain feature properties. The managed builder omits transient generated
numeric MVT IDs, which would collide when independently numbered shards were joined.
Layer names, zoom ranges, geometry and metric properties are retained.

Publication checks include compiler success, PMTiles format/truncation guards,
emitted feature counts, full file checksums and detection of ordinary concurrent
input changes. They do not exhaustively decode every production tile. Real
Tippecanoe tests decode fine tiles containing cells from two H3 base cells and
check retained metrics. Tests also cover pentagons, polar/dateline cells, holes,
overlapping boundaries, stale inputs, missing partitions, interrupted shards and
merges, unchanged reruns, corrupted archives, and pinning the source generation.

Budget space for final archives, earlier generations, compiler scratch and optional
cached shards. Automatic pruning is not implemented. DuckDB's memory limit does
not cap Shapely, Arrow or Tippecanoe. Defaults are 2,048-cell batches, one DuckDB
thread and a 750 MB DuckDB limit. Compiler threads follow `PIPELINE_WORKERS`
(CPU/memory-aware `auto` by default), with `TIPPECANOE_MAX_THREADS` or
`--tile-threads` overrides. The benchmark above explicitly used two compiler
threads. For example:

```bash
just data-tiles --batch-size 1024 --tile-threads 4 --memory-limit 1GB
```

## Reproduce

Use an existing wide metric partition and a new output directory:

```bash
uv run python -m archive.research.profile_tile_export \
  --source /path/to/res7_aggregates/base_12.parquet \
  --output-dir /tmp/ark-tile-profile-new \
  --repeats 3 --cprofile --compile
```

To reproduce the original boundary scope after installing global ADM2, set
`MUNICIPALITY_BOUNDARIES_PATH=app/static/data/boundaries/municipality.geojson`.

The harness records timing, sampled Python RSS, hashes, separate `.prof` files,
text profiles and compiler artifacts. It intentionally writes GeoJSON for exact
comparison and isolated compiler timing. Every subprocess has a timeout.
The tracked summary is
archive/research/tile_export_profile_2026-09-02.json (local-only `archive/research/tile_export_profile_2026-09-02.json`).
Raw local profiles are under ignored `data/spatial-test/tile-profiles/`.
