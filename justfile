set dotenv-load

postgres_admin_url := env_var_or_default("POSTGRES_ADMIN_URL", "postgres")
postgres_app_user := env_var_or_default("POSTGRES_APP_USER", "ark")
postgres_app_password := env_var_or_default("POSTGRES_APP_PASSWORD", "ark")
postgres_app_database := env_var_or_default("POSTGRES_APP_DATABASE", "ark_iv")
global_data_root := env_var_or_default("GLOBAL_DATA_ROOT", "./data/external")
global_h3_root := env_var_or_default("GLOBAL_H3_ROOT", global_data_root / "h3_aggregated")
global_preview_root := env_var_or_default("GLOBAL_PREVIEW_ROOT", global_data_root / "ark_iv_global_preview")
global_crosswalk_path := env_var_or_default("GLOBAL_CROSSWALK_PATH", "data/exports/iucn_goat_global/iucn_goat_crosswalk.parquet")
global_database_url := env_var_or_default("GLOBAL_DATABASE_URL", "postgresql://ark:ark@127.0.0.1:5432/ark_iv_global")

# Show available commands.
default:
    @just --list

# Install Python, lint, and frontend dependencies.
[group('01 - Setup and run')]
install:
    uv sync --group dev
    cd frontend && bun install --frozen-lockfile

# Create/configure the local Postgres role, database, and public schema.
# POSTGRES_ADMIN_URL must connect as a role allowed to create roles/databases.
[group('06 - Compatibility and internals')]
db-configure:
    #!/usr/bin/env zsh
    set -euo pipefail
    psql '{{ postgres_admin_url }}' \
      -v ON_ERROR_STOP=1 \
      -v app_user='{{ postgres_app_user }}' \
      -v app_password='{{ postgres_app_password }}' \
      -v app_database='{{ postgres_app_database }}' <<'SQL'
    SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
    WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app_user') \gexec
    SELECT format('CREATE DATABASE %I OWNER %I', :'app_database', :'app_user')
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'app_database') \gexec
    SELECT format('ALTER DATABASE %I OWNER TO %I', :'app_database', :'app_user') \gexec
    \connect :app_database
    SELECT format('ALTER SCHEMA public OWNER TO %I', :'app_user') \gexec
    SELECT format('GRANT ALL ON SCHEMA public TO %I', :'app_user') \gexec
    SQL

# Build Parquet exports and the PMTiles archive with DuckDB/Tippecanoe.
[group('06 - Compatibility and internals')]
build-data:
    uv run python ark_pipeline/builders/coarse_cache.py

# Load the generated Parquet serving tables into Postgres.
[group('06 - Compatibility and internals')]
db-load:
    uv run python -m backend.load_postgres

# One-time complete setup. This intentionally rebuilds data.
[group('06 - Compatibility and internals')]
setup: install db-configure build-data db-load

# Internal server launcher. Dataset profiles set all paths before invoking it.
[group('06 - Compatibility and internals')]
serve:
    #!/usr/bin/env zsh
    set -euo pipefail

    test -f "${PMTILES_PATH:-data/tiles/priorities.pmtiles}" || {
      print -u2 "PMTiles is missing. Run 'just setup' once first."
      exit 1
    }
    test -f "${MAP_METADATA_PATH:-data/tiles/map-metadata.json}" || {
      print -u2 "Map metadata is missing. Run 'just build-data' once first."
      exit 1
    }

    api_host="${API_HOST:-127.0.0.1}"
    frontend_host="${FRONTEND_HOST:-127.0.0.1}"
    read -r api_port frontend_port < <(
      uv run python scripts/select_dev_ports.py \
        --api-host "$api_host" \
        --api-port "${API_PORT:-8000}" \
        --frontend-host "$frontend_host" \
        --frontend-port "${FRONTEND_PORT:-5173}"
    )

    url_host() {
      case "$1" in
        0.0.0.0|::|\[::\]) print '127.0.0.1' ;;
        *:*) print "[$1]" ;;
        *) print "$1" ;;
      esac
    }
    api_url="http://$(url_host "$api_host"):$api_port"
    frontend_url="http://$(url_host "$frontend_host"):$frontend_port"

    export API_PORT="$api_port"
    export FRONTEND_PORT="$frontend_port"
    export BACKEND_PROXY_TARGET="$api_url"
    export FRONTEND_ORIGIN="$frontend_url"

    print "Ark-IV development session"
    print "  Frontend: $frontend_url"
    print "  API:      $api_url"

    uv run granian \
      --interface asgi \
      --host "$api_host" \
      --port "$API_PORT" \
      --workers "${API_WORKERS:-1}" \
      backend.app:app &
    api_pid=$!

    cleanup() {
      trap - EXIT
      kill -TERM "$api_pid" 2>/dev/null || true
      wait "$api_pid" 2>/dev/null || true
    }
    trap cleanup EXIT
    trap 'exit 0' INT TERM

    cd frontend
    bun run dev -- \
      --host "$frontend_host" \
      --port "$FRONTEND_PORT" \
      --strictPort

# Start the global dataset.
[group('01 - Setup and run')]
start: global-start

[group('01 - Setup and run')]
dev: start

# Run pipeline unit tests.
[group('03 - Validation')]
test:
    uv run python -m unittest discover -s tests -v

# Lint Python source and tests.
[group('03 - Validation')]
lint:
    uv run ruff check .

# Type-check, test, and build the frontend.
[group('03 - Validation')]
frontend-check:
    cd frontend && bun run check
    cd frontend && bun test
    cd frontend && bun run build

# Check files that would be included in the next commit.
[group('03 - Validation')]
release-check:
    uv run python scripts/check_release.py

# Also check all local refs for known restricted database artifacts.
[group('03 - Validation')]
release-history-check:
    uv run python scripts/check_release.py --history

# Run the complete repository verification suite.
[group('03 - Validation')]
check: lint test frontend-check release-check

# Validate the configured H3 list files without requiring a crosswalk.
[group('03 - Validation')]
validate-h3:
    uv run python -m ark_pipeline.cli.spatial_validate \
      --res3 "${H3_RES3_PARQUET:-data/h3_res3_species.parquet}" \
      --res7 "${H3_RES7_PARQUET:-data/h3_res7_species.parquet}" \
      --output "${H3_VALIDATION_REPORT_PATH:-data/validation/h3-input.json}"

# Bootstrap sources and pause cleanly for authorized IUCN browser downloads.
[group('02 - Data workflow')]
download:
    uv run python -m ark_pipeline.cli.sources_sync download --root "{{ global_data_root }}"

# Refresh only missing or due releases; current files are never downloaded again.
[group('02 - Data workflow')]
update:
    uv run python -m ark_pipeline.cli.sources_sync update --root "{{ global_data_root }}"

# Show source and stage readiness without downloading or rebuilding anything.
[positional-arguments]
[group('02 - Data workflow')]
data-status *args:
    uv run python -m ark_pipeline.cli.spatial_aggregate status --root "{{ global_data_root }}" "$@"

# Complete data build: acquisition, source checks, pairs, lists, metrics and tiles.
[positional-arguments]
[group('02 - Data workflow')]
data-build *args:
    uv run python -m ark_pipeline.cli.serving_prepare --root "{{ global_data_root }}" --acquire download --crosswalk-mode refresh --build-pairs --tiles "$@"

# Refresh due sources, then resume the complete data build through map tiles.
[positional-arguments]
[group('02 - Data workflow')]
data-update *args:
    uv run python -m ark_pipeline.cli.serving_prepare --root "{{ global_data_root }}" --acquire update --crosswalk-mode refresh --build-pairs --tiles "$@"

# After acquisition: carry the stratified polygon fixture through every build stage.
# Point/HydroBASINS phases are exercised by production builds but not yet projected here.
[positional-arguments]
[group('02 - Data workflow')]
data-benchmark *args:
    uv run python -m ark_pipeline.cli.benchmark_pipeline --root "{{ global_data_root }}" "$@"

# After pair generation: aggregation, metadata, coarse serving data, and fine metrics.
# Automatically selects this profile's serving generation; accepts --dry-run.
[positional-arguments]
[group('02 - Data workflow')]
data-prepare *args:
    uv run python -m ark_pipeline.cli.serving_prepare --root "{{ global_data_root }}" "$@"

# Validate/build pairs, then aggregate directly into serving lists.
[group('04 - Spatial stages')]
spatial: spatial-build data-aggregate

# Resume pair deduplication and species-list aggregation; no crosswalk required.
[positional-arguments]
[group('02 - Data workflow')]
data-aggregate *args:
    uv run python -m ark_pipeline.cli.spatial_aggregate --root "{{ global_data_root }}" "$@"

# Validate IUCN polygons, points, basin tables, and HydroBASINS geometry.
[group('04 - Spatial stages')]
spatial-doctor:
    uv run python -m ark_pipeline.cli.spatial_pairs doctor --root "{{ global_data_root }}" \
      --output "{{ global_data_root }}/validation/spatial-inputs.json"

# Build or resume polygon, point, and HydroBASINS resolution-7 pairs.
[group('04 - Spatial stages')]
spatial-build:
    uv run python -m ark_pipeline.cli.spatial_pairs build --root "{{ global_data_root }}"

# Globally deduplicate resolution-7 pairs and derive distinct resolution-3 pairs.
[group('04 - Spatial stages')]
spatial-finalize:
    uv run python -m ark_pipeline.cli.spatial_pairs finalize --root "{{ global_data_root }}"

# Compatibility alias for the direct pair-to-list aggregation stage.
[group('04 - Spatial stages')]
spatial-export: data-aggregate

# Build both coarse and fine serving products from the selected H3 dataset.
[group('05 - Serving stages')]
global-prepare: global-build-preview global-res7-aggregate
    uv run python -m ark_pipeline.cli.serving_tiles record \
      --h3-root "{{ global_h3_root }}" \
      --parts-dir "{{ global_preview_root }}/res7_aggregates" \
      --build-duckdb data/global/build.duckdb \
      --species data/global/species/species.parquet \
      --species-systems data/global/species/species_systems.parquet \
      --metadata-template data/global/tiles/map-metadata.json \
      --output "{{ global_preview_root }}/prepared-inputs.json"

# Build the global resolution-3 preview without running the expensive lossless audit.
[group('05 - Serving stages')]
global-build-preview: data-boundaries
    uv run python -m ark_pipeline.cli.serving_metadata \
      --root "{{ global_data_root }}" --h3-root "{{ global_h3_root }}" \
      --crosswalk "{{ global_crosswalk_path }}" --output-dir data/global/species
    uv run python ark_pipeline/builders/source_database.py \
      --target data/global/source.duckdb --overwrite --resolutions 3 \
      --species-parquet data/global/species/species.parquet \
      --species-systems-parquet data/global/species/species_systems.parquet \
      --h3-res3 "{{ global_h3_root }}/h3_res3_species_global_merged.parquet" \
      --crosswalk "{{ global_crosswalk_path }}" \
      --defer-lossless-validation
    SOURCE_DUCKDB_PATH=./data/global/source.duckdb \
      BUILD_DUCKDB_PATH=./data/global/build.duckdb \
      EXPORT_DIR=./data/global/exports TILE_DIR=./data/global/tiles \
      PMTILES_PATH=./data/global/tiles/priorities.pmtiles \
      MAP_METADATA_PATH=./data/global/tiles/map-metadata.json \
      VALIDATION_REPORT_PATH=./data/global/build-validation.json \
      uv run python ark_pipeline/builders/coarse_cache.py --rebuild-aggregates --resolutions 3 \
      --skip-expanded-cell-species --defer-lossless-validation

# Create the dedicated global serving database.
[group('05 - Serving stages')]
global-db-configure:
    #!/usr/bin/env zsh
    set -euo pipefail
    psql '{{ postgres_admin_url }}' -v ON_ERROR_STOP=1 \
      -v app_user='{{ postgres_app_user }}' <<'SQL'
    SELECT format('CREATE DATABASE ark_iv_global OWNER %I', :'app_user')
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ark_iv_global') \gexec
    SQL

# Load the compact global preview exports into the separate preview database.
[group('05 - Serving stages')]
global-db-load:
    DATABASE_URL='{{ global_database_url }}' EXPORT_DIR=./data/global/exports \
      uv run python -m backend.load_postgres

# Resume global resolution-7 aggregation; stale metric schemas rebuild automatically.
[positional-arguments]
[group('05 - Serving stages')]
global-res7-aggregate *args:
    uv run python -m ark_pipeline.builders.fine_metrics aggregate \
      --parts-dir "{{ global_h3_root }}/res7_merged_parts" \
      --species data/global/species/species.parquet \
      --species-systems data/global/species/species_systems.parquet \
      --output-dir "{{ global_preview_root }}/res7_aggregates" \
      --scratch-dir "{{ global_preview_root }}/scratch" \
      --memory-limit "${DUCKDB_MEMORY_LIMIT:-750MB}" "$@"

# Resume a full PMTiles snapshot from the exact recorded preparation generation.
[positional-arguments]
[group('02 - Data workflow')]
data-tiles *args:
    uv run python -m ark_pipeline.cli.serving_tiles build \
      --prepared-inputs "{{ global_preview_root }}/prepared-inputs.json" \
      --output-dir "{{ global_preview_root }}/tiles" \
      --scratch-dir "{{ global_preview_root }}/scratch/tippecanoe" "$@"

# Compatibility alias for the managed static snapshot build.
[group('05 - Serving stages')]
global-res7-tiles: data-tiles

# Acquire the pinned worldwide ADM2 snapshot and install country-sized catalogues.
[group('02 - Data workflow')]
data-boundaries:
    uv run python -m ark_pipeline.cli.sources_acquire update --root "{{ global_data_root }}" --source geoboundaries-adm2
    uv run python -m ark_pipeline.cli.boundaries_prepare --root "{{ global_data_root }}"

# Start the already-built global dataset.
[group('06 - Compatibility and internals')]
global-start:
    #!/usr/bin/env zsh
    set -euo pipefail
    source_parts="{{ global_h3_root }}/res7_merged_parts"
    aggregate_parts="{{ global_preview_root }}/res7_aggregates"
    test -d "$source_parts" || {
      print -u2 "Global resolution-7 source partitions are unavailable: $source_parts"
      exit 1
    }
    test -d "$aggregate_parts" || {
      print -u2 "Global resolution-7 aggregate partitions are unavailable: $aggregate_parts"
      exit 1
    }
    DATABASE_URL='{{ global_database_url }}' \
      SOURCE_DUCKDB_PATH=./data/global/source.duckdb \
      BUILD_DUCKDB_PATH=./data/global/build.duckdb \
      EXPORT_DIR=./data/global/exports TILE_DIR=./data/global/tiles \
      PMTILES_PATH=./data/global/tiles/priorities.pmtiles \
      MAP_METADATA_PATH=./data/global/tiles/map-metadata.json \
      RES7_PARTS_DIR="$source_parts" \
      RES7_AGGREGATE_PARTS_DIR="$aggregate_parts" \
      just serve
