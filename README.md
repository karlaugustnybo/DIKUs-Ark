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
| Build pipeline | DuckDB, GeoPandas/H3, and Tippecanoe with validation reports |

See [the serving schema](docs/schema.md), [global serving
pipeline](docs/global_serving_pipeline.md), and [map performance
retrospective](docs/map-performance-retrospective.md) for the detailed design.

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

`just check` runs Ruff, 44+ Python unit tests, Svelte/type checking, frontend
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
[global source data](docs/global_source_data.md). Boundary sources, licences,
filter semantics, and rebuild commands are in [boundary
filtering](docs/boundary_filtering.md).

## Build and load serving data

With authorized local inputs configured, the ordinary one-time pipeline is:

```bash
just db-configure
just build-data
just db-load
```

Or run `just setup` to install dependencies and perform all three steps.
`POSTGRES_ADMIN_URL` must connect as a role allowed to create the application
role/database. `app/build_cache.py` reads `SOURCE_DUCKDB_PATH`, writes normalized
Parquet exports and score domains, and invokes Tippecanoe for `PMTILES_PATH`.

The database loader skips the expanded `cell_species` relation by default.
Global runtime uses compact `cell_species_lists`; set
`LOAD_EXPANDED_CELL_SPECIES=true` only for a deliberate compatibility workflow.

To construct the mounted global profile:

```bash
just global-build-preview
just global-db-configure
just global-db-load
just global-res7-aggregate
```

The resolution-7 aggregation is resumable and publishes only complete,
schema-validated partitions. `RES7_WORKERS=1` and a smaller
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
