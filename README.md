# Ark-IV

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

Ark-IV is an interactive conservation-priority map that combines extinction
risk, DNA-sequencing evidence, evolutionary distinctiveness, and global species
distributions. The application uses a SvelteKit/deck.gl map, a typed Litestar
API, PostgreSQL for relational reads, PMTiles for coarse global context, and
partitioned Parquet for fine H3 cells.

This repository is a **code release, not a data release**. IUCN and EDGE-backed
records, global species distributions, databases, tiles, and generated serving
artifacts are intentionally excluded. Read [the data publication
policy](DATA_POLICY.md), [source credits](NOTICE.md), and [the publication
checklist](docs/publication_checklist.md) before publishing or deploying data.

Start with the [documentation index](docs/README.md) or the
[pipeline guide](docs/pipeline/01_data_pipeline.md). Pipeline guides are numbered
in reading order. Historical experiments live in a local-only, Git-ignored
`archive/`; a normal clone does not need it.

## Repository layout

| Directory | Contents |
| --- | --- |
| `ark_pipeline/cli/` | Acquisition, spatial processing, orchestration and benchmark entry points |
| `ark_pipeline/builders/` | Serving database, species, metrics and boundary builders |
| `ark_pipeline/spatial/` | Coverage kernels, jurisdiction membership and boundary paths |
| `ark_pipeline/aggregation/` | Pair reduction, species lists and native metrics |
| `ark_pipeline/runtime/` | Resources, progress, estimates, checkpoints and provenance |
| `backend/` | API and PostgreSQL serving |
| `frontend/` | Svelte application and frontend tests |
| `app/static/` | Shared browser assets and checked-in boundary catalogues |
| `config/` | Source inventories and supported spatial profiles |
| `scripts/` | Development ports, release checks and species-search diagnostics |
| `tests/` | Supported Python regression tests |
| `docs/` | Ordered pipeline guides, reference material and performance reports |

`data/`, `acquisition/`, `.tmp/` and `archive/` hold ignored local artifacts.
Do not move data roots or edit provenance receipts to accommodate code changes.
The [pipeline code map](ark_pipeline/README.md) describes each module in stage order.

## Pipeline quick start

The [methodology](methodology.md) documents scientific assumptions, source-field
lineage, geometry processing, taxonomy/DNA rules, scoring, implementation choices
and validation limits. See the [roadmap assumption audit](docs/reference/roadmap_assumption_audit.md)
for the decisions that still require evidence or biological review.

The source workflow is documented in [data acquisition](docs/pipeline/02_data_acquisition.md),
and the spatial-to-H3 stage in [spatial rebuild](docs/pipeline/04_spatial_rebuild.md).
Run `just data-build` for the complete workflow from source acquisition through
range-polygon, point, and HydroBASINS pairs, species lists, metadata,
coarse/fine metrics and static map tiles.
Use `just data-build --dry-run` to preview it and `just data-update` to refresh
sources and run the same workflow. Both automatically build or reuse the
IUCN–GoaT crosswalk from verified IUCN, GoaT and NCBI snapshots before expensive
processing. Uncertain matches remain unresolved and are recorded for review.
`just data-status` provides a read-only source and spatial-output summary.
After acquisition, `just data-benchmark` runs the existing stratified polygon
fixture through all build stages and reports stage timings and a projected total.
That benchmark does not yet project point-CSV or HydroBASINS phases; the full
build dashboard reports their measured work without folding it into a
polygon-only ETA.
Use `--workers 4` to set parallelism, `--max-per-bin 10` for a smaller stratified
subset, or `--dry-run` to preview it. See the [benchmark guide](docs/pipeline/08_pipeline_benchmark.md).
Both commands show a Rich dashboard with incremental progress, benchmark-informed
ETAs, CPU/RAM and worker counts. Progress is checkpointed on Ctrl+C; repeat the
same command to restore compatible unfinished state. Use `--ui plain` for logs
or `--fresh` for a new dashboard history/benchmark run.
Set `PIPELINE_WORKERS=4` in `.env`, or run `just data-build --workers 4`, to share
one parallelism setting across the compute stages. The default `auto` reserves
memory for the desktop. `--metric-workers`, `--duckdb-threads` and
`--tile-threads` provide stage overrides; dry runs show all effective counts.
After generating pairs, `just data-aggregate` builds fine/coarse species lists
without metadata. `just data-prepare` continues through aggregation,
metadata, the coarse map, and fine metrics using the selected spatial profile.
It requires a current crosswalk; add `--crosswalk-mode refresh` to select the
automatic crosswalk workflow. `just data-prepare --dry-run` shows its plan.
Use `just data-prepare --tiles` to finish with a static PMTiles archive, or
`just data-tiles` to resume only the export. The archive and map metadata are
published together under `GLOBAL_PREVIEW_ROOT/tiles/current/`.
Global ADM2 boundaries (49,308 areas across 180 available countries) are installed
with `just data-boundaries`, also run during global map preparation. Country-sized
catalogues keep the worldwide geometry out of browser downloads.
The [pipeline guide](docs/pipeline/01_data_pipeline.md) explains the crosswalk and database
handoff. `just download` and `just update` remain available for acquisition only.

## Current state

- Global resolution-3 context is parsed once from a compact Arrow snapshot and
  preloaded only during browser idle time or explicit map-link intent.
- Fine resolution-7 cells are requested in compact typed arrays, committed as
  complete viewport generations, and cached by canonical web tile.
- Country filtering includes the country's Marine Regions EEZ membership.
  EEZ is therefore not duplicated as a separate filter.
- Species search is ordinary case/accent-insensitive text search with ranked
  exact, prefix, substring, and typo-tolerant matches.
- Map colour domains support species-count normalization and selected
  jurisdiction scopes.
- CI runs Python lint/tests, Svelte checks, frontend tests, the production build,
  and a release-content guard.

Remaining product and research work is tracked in [the roadmap](docs/roadmap.md).

## Architecture

| Layer | Implementation |
| --- | --- |
| Browser | SvelteKit 5, TypeScript, Tailwind CSS, MapLibre, deck.gl |
| Coarse map | One global resolution-3 Arrow snapshot; PMTiles remains the build/archive format |
| Fine map | On-demand resolution-7 arrays from spatially sorted Parquet partitions |
| API | Litestar served by Granian, with a checked-in OpenAPI contract |
| Relational serving | PostgreSQL with compact cell/species and inverse species/cell arrays |
| Build pipeline | GDAL Arrow streams, native H3/GEOS, DuckDB, and Tippecanoe with validation reports |

See [the serving schema](docs/reference/schema.md), [global serving
pipeline](docs/pipeline/07_global_serving_pipeline.md), and [map performance
retrospective](docs/performance/map-performance-retrospective.md) for the detailed design.

## Prerequisites

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- [Bun](https://bun.sh/) 1.3.9 or newer
- PostgreSQL 16 or newer for API runtime and integration work
- [Tippecanoe](https://github.com/felt/tippecanoe) when rebuilding PMTiles
- [Just](https://just.systems/) for the documented commands

On macOS these can be installed with Homebrew; Python and frontend dependencies
are resolved exclusively through uv and Bun.

## Install and verify the code release

A normal clone can install, lint, test, and build without the restricted data:

```bash
cp .env.example .env
cp frontend/.env.example frontend/.env
just install
just check
```

`just check` runs Ruff, Python unit tests, Svelte/type checking, frontend
unit tests, a production build, and the prospective-commit release guard. It
does not require a running PostgreSQL server or the global external-data disk.

## Configure authorized local data

Environment files hold all database credentials and source/generated paths and
are ignored by Git. The examples contain local-only defaults:

```bash
cp .env.example .env
cp frontend/.env.example frontend/.env
```

Keep authorized databases under `data/private/` or outside the repository.
Keep the global raw and generated datasets under `GLOBAL_DATA_ROOT` and
`GLOBAL_PREVIEW_ROOT`. Never weaken `.gitignore` to make a build work.

The complete global source inventory and identifier contract are in
[global source data](docs/reference/global_source_data.md). Boundary sources, licences,
filter semantics, and rebuild commands are in [boundary
filtering](docs/reference/boundary_filtering.md).

## Build and load serving data

With authorized local inputs configured, the ordinary one-time pipeline is:

```bash
just db-configure
just build-data
just db-load
```

Or run `just setup` to install dependencies and perform all three steps.
`POSTGRES_ADMIN_URL` must connect as a role allowed to create the application
role/database. `ark_pipeline/builders/coarse_cache.py` reads `SOURCE_DUCKDB_PATH`, writes normalized
Parquet exports and score domains, and invokes Tippecanoe for `PMTILES_PATH`.

The database loader skips the expanded `cell_species` relation by default.
Global runtime uses compact `cell_species_lists`; set
`LOAD_EXPANDED_CELL_SPECIES=true` only for a deliberate compatibility workflow.

To construct the global serving profile, select `GLOBAL_H3_ROOT` and a reviewed
crosswalk as described in the [pipeline guide](docs/pipeline/01_data_pipeline.md), then run:

```bash
just global-prepare
just global-db-configure
just global-db-load
```

The resolution-7 aggregation is resumable and publishes only complete,
schema-validated partitions. Its native batch reducer was
[3.23× faster on a 3-million-relationship sample](docs/performance/serving_metrics_performance.md),
with identical metric output. `RES7_WORKERS=1` and a smaller
`DUCKDB_MEMORY_LIMIT` provide a bounded mode for memory-constrained machines.
The optional `just global-res7-tiles` creates a much larger static snapshot;
normal serving does not require it.

## Run locally

Once the global database, resolution-3 snapshot, and external resolution-7
partitions are present:

```bash
just start
```

The launcher checks the required paths, starts Granian and Vite, and chooses
free API/frontend ports when the configured ones are occupied. It prints the
two final URLs and keeps the proxy and CORS origins aligned for that session.
Stop both processes with `Ctrl-C`.

To run services separately, load `.env` and start the API:

```bash
set -a
source .env
set +a
uv run granian --interface asgi --host "$API_HOST" --port "$API_PORT" --workers "$API_WORKERS" backend.app:app
```

Then start the frontend in another terminal:

```bash
cd frontend
bun run dev
```

API documentation is available at `/schema` on the backend URL.

## OpenAPI contract

After changing routes or response models, regenerate both checked-in outputs:

```bash
uv run python -m backend.export_openapi
cd frontend
bun run generate:api
```

Frontend code imports generated types from
`frontend/src/lib/api/schema.d.ts`; it does not maintain a second handwritten
API model.

## Production and performance

Build the static frontend with `bun run build`. Serve immutable static assets
through a CDN/object store and route `/api` to a multi-worker Granian deployment.
The Litestar static routes are suitable for local or simple single-host use.

The map includes an opt-in deterministic trace:

```text
/map?mapPerf=1&mapPerfTrace=1&mapPerfRuns=3
```

It measures first-path loading and identical cached repeats, including frame
cadence, p95/worst frames, long tasks, fine-tile requests, loading frames,
uncovered viewports, and resolution flashes. A release should retain zero
uncovered frames and zero flashes and should be compared with a same-session
idle cadence; the complete method is documented in the performance
retrospective.

## Licensing

Ark-IV's application code is licensed under the GNU Affero General Public
License v3.0 only (`AGPL-3.0-only`); see [LICENSE](LICENSE). Operators that
modify the application and make it available over a network must offer users
the corresponding source for that running version under the AGPL.

The code licence does **not** apply to source datasets or generated data
artifacts. Those retain their own terms and required credits; see `NOTICE.md`
and `DATA_POLICY.md`. In particular, AGPL does not grant permission to publish
IUCN, EDGE, or other restricted data alongside the application.
