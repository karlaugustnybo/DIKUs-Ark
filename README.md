# Ark-IV

Ark-IV combines IUCN, GoaT, EDGE and species-distribution data into an interactive conservation-priority map. The SvelteKit interface preserves the original visual design while moving the serving path to Litestar, Postgres and static PMTiles.

## Architecture

- **Browser:** SvelteKit, Tailwind CSS and deck.gl `MVTLayer`.
- **Map:** a Tippecanoe-built `priorities.pmtiles` archive plus a small generated global score-domain JSON file. Pan and zoom read static files; they do not query the database.
- **API:** typed Litestar endpoints served by Granian. Litestar publishes the OpenAPI contract used to generate the frontend's TypeScript types.
- **Serving data:** Postgres with ordinary B-tree indexes on integer H3 keys.
- **Build data:** DuckDB transforms the source database into PMTiles and Parquet. DuckDB is not used by HTTP requests.

## Prerequisites

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- [Bun](https://bun.sh/) 1.3 or newer
- Postgres 16 or newer
- [Tippecanoe](https://github.com/felt/tippecanoe) for rebuilding PMTiles

## Configuration

All ports, database credentials, source locations and generated-data locations come from environment files:

```bash
cp .env.example .env
cp frontend/.env.example frontend/.env
```

Edit `.env` for the local Postgres connection and data locations. The frontend defaults to same-origin API/static requests in production and proxies them to the configured backend during development.

## Install and build

The complete one-time setup is available as a Just recipe:

```bash
cp .env.example .env
cp frontend/.env.example frontend/.env
just setup
```

`POSTGRES_ADMIN_URL` must identify a local Postgres administrator. The recipe creates or repairs the application role, database ownership, and `public` schema permissions before loading data.

The equivalent individual commands are:

```bash
uv sync
cd frontend && bun install && cd ..
just db-configure
uv run python app/build_cache.py
uv run python -m backend.load_postgres
```

`app/build_cache.py` reads `SOURCE_DUCKDB_PATH`, writes normalized Parquet exports and the per-system global map score domains, and invokes Tippecanoe to create `PMTILES_PATH`. Pass `--skip-tiles` when only the transfer files and score metadata are needed, or `--rebuild-aggregates` after changing the source database.

## Run locally

For normal development, start both the API and frontend without rebuilding or reloading anything:

```bash
just start
```

`just dev` is an alias for the same command. Stop both processes with `Ctrl-C`.

To start each process separately, load the repository environment and start the API:

```bash
set -a
source .env
set +a
uv run granian --interface asgi --host "$API_HOST" --port "$API_PORT" --workers "$API_WORKERS" backend.app:app
```

In a second terminal:

```bash
cd frontend
bun run dev
```

Open the URL printed by Vite (normally `http://127.0.0.1:5173`). API documentation is available at `/schema` on the backend.

## OpenAPI type sharing

After changing API routes or response models, regenerate the checked-in contract and frontend declarations:

```bash
uv run python -m backend.export_openapi
cd frontend
bun run generate:api
```

Svelte code imports types from `src/lib/api/schema.d.ts`; it does not maintain a separate set of handwritten API interfaces.

## Production

Build the static frontend with `bun run build`. Serve the frontend, PMTiles archive and generated map metadata through a CDN/object store such as Cloudflare R2 or MinIO, and route `/api` to a multi-worker Granian deployment. The Litestar static routes are intended for local development and simple single-host deployments.

## Source database

`app/build_db.py` can reconstruct the DuckDB source database from the raw inputs configured in `.env`. `app/build_h3_aggregate.py` performs the large, disk-backed H3 aggregation; its input, output, scratch directory, memory and thread count are also environment-controlled.

The global H3 adapter, crosswalk contract, streaming tile build, and
losslessness reports are documented in
[`docs/global_serving_pipeline.md`](docs/global_serving_pipeline.md).

The original relational diagrams remain available in `readme_imgs/Ark-IV_ER.png` and `readme_imgs/Ark-IV_Schemas.png`.
